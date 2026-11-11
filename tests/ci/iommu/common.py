"""Shared, platform-independent CI primitives."""
import hashlib
import json
import os
from pathlib import Path
import selectors
import shlex
import signal
import subprocess
import time

HERE = Path(__file__).resolve().parent
SOURCE = HERE.parents[2]


def read_json(path):
    return json.loads(Path(path).read_text())


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def source_identity():
    tracked = subprocess.check_output(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=SOURCE)
    digest = hashlib.sha256()
    for name in sorted(set(tracked.split(b"\0")) - {b""}):
        path = SOURCE / os.fsdecode(name)
        if path.is_file():
            digest.update(name + b"\0" + sha256(path).encode() + b"\0")
    return {"commit": subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=SOURCE, text=True).strip(),
        "content_sha256": digest.hexdigest()}


def kill_group(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
        # Reap remaining grandchildren even if the immediate child exited.
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def run_logged(argv, log, cwd=None, env=None, timeout=3600, check=True):
    """Keep complete logs, bounded live output, and an absolute deadline."""
    log = Path(log)
    log.parent.mkdir(parents=True, exist_ok=True)
    write_json(log.with_suffix(log.suffix + ".command.json"),
               {"argv": list(map(str, argv)), "cwd": str(cwd or SOURCE)})
    print("+ " + shlex.join(list(map(str, argv))), flush=True)
    started = time.monotonic()
    limit = int(os.environ.get("IOMMU_CI_LIVE_LOG_KIB", "256")) * 1024
    printed = 0
    with log.open("wb") as output:
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        try:
            while selector.get_map():
                if time.monotonic() - started > timeout:
                    raise TimeoutError(f"process exceeded {timeout}s: {log.name}")
                for key, _ in selector.select(timeout=0.2):
                    data = os.read(key.fileobj.fileno(), 65536)
                    if not data:
                        selector.unregister(key.fileobj)
                        continue
                    output.write(data)
                    output.flush()
                    if printed < limit:
                        # UART through a PTY can produce CR CR LF. GitLab's
                        # trace renderer drops those lines; keep the artifact
                        # raw and use LF line endings only for live display.
                        visible = data[:limit - printed].decode(errors="replace").replace("\r", "")
                        print(visible, end="", flush=True)
                    printed += len(data)
            code = process.wait(timeout=max(0.1, timeout - (time.monotonic() - started)))
        finally:
            selector.close()
            kill_group(process)
            process.stdout.close()
    if check and code:
        raise RuntimeError(f"process exited {code}; see {log}")
    return code


def simulator_environment():
    """Use the runner environment without loading a licensed simulator."""
    env = dict(os.environ)
    if verible_dir := env.get("IOMMU_CI_VERIBLE_DIR"):
        env["PATH"] = os.pathsep.join(path for path in (verible_dir, env.get("PATH", "")) if path)
    env.pop("LD_PRELOAD", None)
    env.pop("IO_SYSTEM_IOMMU_RTL_HW_BYPASS", None)
    return env
