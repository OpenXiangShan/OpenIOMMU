"""Fresh builds and relocatable runtime bundles."""
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys

from assets import check_asset_access, extract, get_asset
from common import HERE, SOURCE, read_json, run_logged, sha256, source_identity, simulator_environment, write_json


def resolve_dependency(spec, log):
    # Resolve the remote branch afresh for each pipeline; later jobs fetch the
    # recorded SHA so a moving devel branch cannot change an in-flight build.
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    run_logged(["git", "ls-remote", "--exit-code", "--refs", spec["url"], spec["ref"]],
               log, env=env, timeout=60)
    matches = []
    for line in Path(log).read_text().splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[1] == spec["ref"] and re.fullmatch(r"[0-9a-fA-F]{40}", fields[0]):
            matches.append(fields[0].lower())
    if len(matches) != 1:
        raise ValueError(f"cannot resolve unique QEMU commit for {spec['ref']}; see {log}")
    print(f"QEMU {spec['url']} {spec['ref']} -> {matches[0]}", flush=True)
    return {**spec, "commit": matches[0]}


def doctor(work, pipeline, assets_dir=None):
    errors = []
    tools = ["git", "python3"]
    verilator = {}
    env = simulator_environment()
    if pipeline["selected"]:
        tools += ["cc", "c++", "cmake", "make", "ninja", "pkg-config", "dtc", "ldd",
                  "verible-verilog-syntax"]
        tools.append("verilator")
        if shutil.which("verilator", path=env.get("PATH")):
            try:
                root = subprocess.check_output(
                    ["verilator", "--getenv", "VERILATOR_ROOT"], text=True, timeout=30,
                    env=env).strip()
                verilator = {"executable": shutil.which("verilator", path=env.get("PATH")), "root": root,
                             "version": subprocess.check_output(
                                 ["verilator", "--version"], text=True, timeout=30, env=env).strip()}
                if not root or not (Path(root) / "include/vltstd/svdpi.h").is_file():
                    errors.append("missing Verilator svdpi.h")
            except (OSError, subprocess.SubprocessError) as exc:
                errors.append("cannot query Verilator: " + str(exc))
        for dependency in ("glib-2.0", "pixman-1", "zlib"):
            if subprocess.run(["pkg-config", "--exists", dependency], check=False).returncode:
                errors.append("missing development package: " + dependency)
        try:
            import pexpect  # noqa: F401
        except ImportError:
            errors.append("install requirements.txt (pexpect)")
    for tool in tools:
        if not shutil.which(tool, path=env.get("PATH")):
            hint = " (set IOMMU_CI_VERIBLE_DIR)" if tool == "verible-verilog-syntax" else ""
            errors.append("missing executable: " + tool + hint)
    if sys.version_info < (3, 12):
        errors.append("Python >= 3.12 required (safe archive extraction)")
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        errors.append("requires Linux x86_64")
    jfrog_access = {"checked": False}
    if pipeline["selected"] and not assets_dir:
        try:
            spec = read_json(HERE / "assets.lock.json")["assets"]["picker_sdk"]
            status = check_asset_access(spec)
            jfrog_access = {"checked": True, "status": status}
            print(f"JFrog download access: PASS (HTTP {status})", flush=True)
        except (ValueError, RuntimeError) as exc:
            errors.append(str(exc))
            jfrog_access = {"checked": True, "status": None, "error": str(exc)}
    evidence = {"errors": errors, "python": sys.version,
                "platform": platform.platform(), "verilator": verilator,
                "free_bytes": shutil.disk_usage(work).free, "jfrog_access": jfrog_access}
    write_json(work / "doctor.json", evidence)
    if errors:
        raise RuntimeError("preflight failed: " + "; ".join(errors))


def build(work, pipeline, jobs, assets_dir=None):
    if not pipeline["selected"]:
        return
    if (work / "qemu-build").exists() or (work / "picker-build").exists():
        raise ValueError("build output exists; create a new work-dir")
    specs = read_json(HERE / "assets.lock.json")["assets"]
    assets = {name: get_asset(work, name, spec, assets_dir) for name, spec in specs.items()}
    extract(assets["picker_sdk"], work / "tools")
    sdk = work / "tools/sdk"
    tool_info = read_json(sdk / "provenance.json")
    if tool_info["source_commit"] != specs["picker_sdk"]["source_commit"] or not tool_info["coroutine"]:
        raise ValueError("SDK provenance/ABI mismatch")
    qemu = work / "deps/qemu"
    qemu.parent.mkdir(parents=True, exist_ok=True)
    dependency = pipeline["dependencies"]["qemu"]
    run_logged(["git", "init", str(qemu)], work / "logs/qemu-init.log")
    run_logged(["git", "fetch", "--depth=1", dependency["url"], dependency["commit"]],
               work / "logs/qemu-fetch.log", cwd=qemu, timeout=600)
    run_logged(["git", "checkout", "--detach", "FETCH_HEAD"], work / "logs/qemu-checkout.log", cwd=qemu)
    # Use the QEMU wrap revision as well as the locked archive digest.
    for name in ("berkeley-softfloat-3", "berkeley-testfloat-3", "keycodemapdb"):
        wrap = (qemu / "subprojects" / (name + ".wrap")).read_text()
        if specs[name]["revision"] not in wrap:
            raise ValueError("QEMU wrap changed; update locked native dependency: " + name)
        extract(assets[name], qemu / "subprojects")
        overlays = qemu / "subprojects/packagefiles" / name
        if overlays.is_dir():
            shutil.copytree(overlays, qemu / "subprojects" / name, dirs_exist_ok=True)
    env = simulator_environment()
    env["PATH"] = str(sdk / "bin") + ":" + env["PATH"]
    env["LD_LIBRARY_PATH"] = str(sdk / "share/picker/lib") + ":" + env.get("LD_LIBRARY_PATH", "")
    picker = work / "picker-build"
    model_threads = int(os.environ.get("IOMMU_CI_MODEL_THREADS", "8"))
    if model_threads < 1:
        raise ValueError("IOMMU_CI_MODEL_THREADS must be positive")
    run_logged(["bash", str(HERE / "build_picker.sh"), str(SOURCE), str(picker), str(sdk),
                str(jobs), str(model_threads)],
               work / "logs/picker-build.log", env=env, timeout=5400)
    io_system = qemu / "third_party/io-system-lib"
    api_output = work / "iommu-api-output"
    make_args = ["make", "-C", str(io_system), "iommu-rtl-api",
                 "BUILD_DIR=" + str(work / "iommu-api-build"), "OUTPUT_DIR=" + str(api_output),
                 "IO_SYSTEM_IOMMU_RTL=1", "IOMMU_RTL_PICKER_OUT=" + str(picker),
                 "PICKER_XSPCOMM_INCLUDE=" + str(sdk / "share/picker/include"),
                 "PICKER_XSPCOMM_LIB=" + str(sdk / "share/picker/lib/libxspcomm.so"),
                 "IOMMU_RTL_SIMULATOR=verilator", "-j" + str(jobs)]
    run_logged(make_args, work / "logs/iommu-api-build.log", env=env)
    bundle = work / "bundle"
    bundle.mkdir()
    if pipeline["source"] != source_identity():
        raise ValueError("source changed during build; use a new work-dir")
    shutil.copytree(api_output / "iommu-rtl", bundle / "iommu")
    shutil.copytree(io_system / "src/iommu_api/include", bundle / "include")
    run_logged([os.environ.get("CXX", "c++"), "-std=c++20", "-O2", "-Wall", "-Wextra", "-Werror",
                str(HERE / "rtl_cases.cpp"), "-I" + str(bundle / "include"),
                "-L" + str(bundle / "iommu"), "-Wl,-rpath,$ORIGIN/iommu", "-liommu_api",
                "-o", str(bundle / "rtl-cases")], work / "logs/rtl-cases-build.log", env=env)
    qemu_build = work / "qemu-build"
    qemu_build.mkdir()
    configure = [str(qemu / "configure"), "--target-list=riscv64-softmmu",
                 "--enable-tcg", "--disable-kvm", "--disable-docs", "--disable-werror",
                 "--disable-download", "--disable-gtk", "--disable-sdl", "--disable-vnc",
                 "--disable-guest-agent", "--disable-tools", "--disable-rust",
                 "--enable-my-virtio", "--enable-io-system", "--disable-io-system-rtl-system",
                 "--enable-io-system-iommu-rtl", "--io-system-iommu-simulator=verilator",
                 "--io-system-iommu-picker-out=" + str(picker),
                 "--io-system-picker=" + str(sdk / "bin/picker"),
                 "--io-system-picker-template=" + str(sdk / "share/picker/template"),
                 "--io-system-picker-xspcomm-include=" + str(sdk / "share/picker/include"),
                 "--io-system-picker-xspcomm-lib=" + str(sdk / "share/picker/lib/libxspcomm.so")]
    run_logged(configure, work / "logs/qemu-configure.log", cwd=qemu_build, env=env)
    run_logged(["ninja", "-j", str(jobs), "qemu-system-riscv64"],
               work / "logs/qemu-build.log", cwd=qemu_build, env=env, timeout=5400)
    shutil.copyfile(qemu_build / "qemu-system-riscv64", bundle / "qemu-system-riscv64")
    (bundle / "qemu-system-riscv64").chmod(0o755)
    shutil.copytree(qemu / "pc-bios", bundle / "pc-bios")
    # Common inputs travel once in the bundle, never depend on another job's
    # absolute checkout or on a source rootfs modified by a guest.
    for name in ("kernel", "fw_jump", "rootfs"):
        shutil.copyfile(assets[name], bundle / name)
    if pipeline["source"] != source_identity():
        raise ValueError("source changed during build; use a new work-dir")
    write_json(bundle / "build.json", {
        "run_id": pipeline["run_id"], "source": pipeline["source"],
        "dependencies": pipeline["dependencies"], "tools": tool_info,
        "simulator": "verilator", "model_threads": model_threads, "coverage": False,
        "verilator_version": subprocess.check_output(
            ["verilator", "--version"], text=True, env=env, timeout=30).strip(),
        "files": {str(p.relative_to(bundle)): sha256(p) for p in sorted(bundle.rglob("*")) if p.is_file()},
    })


def verify_bundle(work, pipeline):
    bundle = work / "bundle"
    info = read_json(bundle / "build.json")
    if info["run_id"] != pipeline["run_id"] or info["source"] != pipeline["source"] or info["dependencies"] != pipeline["dependencies"]:
        raise ValueError("stale or mismatched bundle provenance")
    if not info.get("files"):
        raise ValueError("missing bundle checksum manifest")
    for name, digest in info["files"].items():
        path = (bundle / name).resolve()
        if not path.is_relative_to(bundle.resolve()) or not path.is_file() or sha256(path) != digest:
            raise ValueError("bundle checksum mismatch: " + name)
    return bundle


def runtime_env(bundle):
    env = simulator_environment()
    info = read_json(bundle / "build.json")
    libs = bundle / "iommu"
    if info.get("simulator") != "verilator" or (libs / ".simulator").read_text().strip() != "verilator":
        raise ValueError("runtime bundle is not a Verilator build")
    env["LD_LIBRARY_PATH"] = str(libs) + ":" + env.get("LD_LIBRARY_PATH", "")
    env["LD_PRELOAD"] = ":".join(str(libs / name) for name in
                                ("libUTiommu_wrap.so", "libxspcomm.so"))
    env["IO_SYSTEM_IOMMU_RTL_NO_WAVE"] = "1"
    env.pop("IO_SYSTEM_IOMMU_RTL_WAVEFORM", None)
    env["IO_SYSTEM_IOMMU_RTL_API_SO"] = str(libs / "libiommu_api.so")
    env.pop("IO_SYSTEM_IOMMU_TRACE", None)
    env.pop("IO_SYSTEM_IOMMU_TRACE_TXN_ID", None)
    return env
