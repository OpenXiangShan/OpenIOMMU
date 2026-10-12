#!/usr/bin/env python3
"""Build an immutable Picker/XSPComm SDK from a clean, pinned XSV archive.

Publication is a separate, explicit operation; ordinary CI never uploads.
"""
import argparse
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile


def command(argv, **kwargs):
    subprocess.run(argv, check=True, **kwargs)


def package(repo, revision, work, jobs):
    commit = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", revision + "^{commit}"], text=True).strip()
    work.mkdir(parents=True, exist_ok=False)
    archive = work / "source.tar"
    with archive.open("wb") as output:
        command(["git", "-C", str(repo), "archive", commit,
                 "contrib/unitychip/picker", "contrib/unitychip/xcomm"], stdout=output)
    with tarfile.open(archive) as stream:
        stream.extractall(work / "source", filter="data")
    source = work / "source/contrib/unitychip"
    sdk = work / "sdk"
    env = dict(os.environ, NO_BUILD_XSPCOMM="1")
    command(["cmake", "-S", str(source / "picker"), "-B", str(work / "picker-build"),
             "-DCMAKE_BUILD_TYPE=Release", "-DCMAKE_INSTALL_PREFIX=" + str(sdk)], env=env)
    command(["cmake", "--build", str(work / "picker-build"), "--target", "picker",
             "--parallel", str(jobs)], env=env)
    command(["cmake", "--install", str(work / "picker-build")], env=env)
    include = sdk / "share/picker/include/xspcomm"
    shutil.copytree(source / "xcomm/include/xspcomm", include)
    config = (include / "xconfig.h.in").read_text()
    for old, new in {"@PROJECT_VERSION@": "0.0.1", "@GIT_HASH@": commit,
                     "@GIT_BRANCH@": "pinned", "@ENABLE_XCOROUTINE@": "true",
                     "@HAVE_EXECINFO_H@": "1"}.items():
        config = config.replace(old, new)
    (include / "xconfig.h").write_text(config)
    objects = work / "xcomm-objects"
    objects.mkdir()
    cxx = os.environ.get("CXX", "c++")
    sources = sorted((source / "xcomm/src").glob("*.cpp"))
    sources = [p for p in sources if p.name != "uvm_pbsb.cpp"]

    def compile_one(path):
        output = objects / (path.stem + ".o")
        command([cxx, "-c", str(path), "-o", str(output), "-O2", "-fPIC", "-std=c++20",
                 "-fcoroutines", "-ftls-model=global-dynamic", "-DHAVE_EXECINFO_H",
                 "-DENABLE_XCOROUTINE=true", '-DXSPCOMM_VERSION="0.0.1"',
                 "-I" + str(include.parent)])
        return output

    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        obj = list(pool.map(compile_one, sources))
    lib = sdk / "share/picker/lib"
    lib.mkdir(parents=True, exist_ok=True)
    command([cxx, "-shared", "-o", str(lib / "libxspcomm.so"),
             *map(str, obj), "-pthread", "-ldl"])
    # This pinned SDK's template exposes `compile`. Add build/install without
    # running its example (which masks failures and may invoke $finish).
    makefile = sdk / "share/picker/template/cpp/Makefile"
    makefile.write_text(makefile.read_text() + "\n" +
                       ".PHONY: build install\nbuild: compile\n"
                       "install: build\n\t@test -f libUT{{__TOP_MODULE_NAME__}}.so\n")
    provenance = {
        "schema": 1, "source_commit": commit,
        "source_repository": subprocess.check_output(
            ["git", "-C", str(repo), "remote", "get-url", "origin"], text=True).strip(),
        "source_archive_sha256": hashlib.file_digest(archive.open("rb"), "sha256").hexdigest(),
        "platform": "linux-x86_64", "coroutine": True,
        "compiler": subprocess.check_output([cxx, "--version"], text=True).splitlines()[0],
        "template_patch": "Add build/install aliases; do not run example",
    }
    licenses = sdk / "licenses"
    licenses.mkdir()
    for name in ("picker", "xcomm"):
        shutil.copyfile(source / name / "LICENSE", licenses / (name + "-LICENSE"))
    (sdk / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
    output = work / ("picker-xspcomm-" + commit[:12] + "-linux-x86_64.tar.gz")
    with tarfile.open(output, "w:gz") as stream:
        stream.add(sdk, arcname="sdk")
    with output.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    spec = {"path": output.name,
            "jf_path": "hejian-workspace/guoyaxing/openiommu-ci/tools/" + commit[:12] + "/" + output.name,
            "size": output.stat().st_size, "sha256": digest, **provenance}
    (work / "tool-spec.json").write_text(json.dumps(spec, indent=2) + "\n")
    print(json.dumps(spec, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-repo", required=True, type=Path)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--jobs", type=int, default=8)
    args = parser.parse_args()
    package(args.source_repo.resolve(), args.revision, args.output.resolve(), args.jobs)
