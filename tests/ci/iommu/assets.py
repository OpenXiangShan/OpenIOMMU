"""One immutable JFrog input contract for local, GitLab and GitHub runs."""
import os
from pathlib import Path
import shutil
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request

from common import sha256, write_json


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("artifact redirect refused; use its final URL")


def verify(path, spec):
    path = Path(path)
    if not path.is_file() or path.stat().st_size != spec["size"] or sha256(path) != spec["sha256"]:
        raise ValueError(f"asset size/SHA256 mismatch: {path.name}")
    return path


def get_asset(work, name, spec, assets_dir=None):
    cache = Path(os.environ.get("IOMMU_CI_CACHE", str(work / "cache")))
    cached = cache / spec["sha256"] / Path(spec["path"]).name
    if assets_dir:
        source = verify(Path(assets_dir) / spec["path"], spec)
        origin = "local-verified"
    elif cached.exists():
        source = verify(cached, spec)
        origin = "cache-verified"
    else:
        base = os.environ.get("IOMMU_CI_JF_BASE_URL", "")
        parsed = urllib.parse.urlsplit(base)
        if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.query or parsed.fragment:
            raise ValueError("IOMMU_CI_JF_BASE_URL must be HTTP(S), without credentials/query")
        relative = spec["jf_path"]
        if relative.startswith("/") or ".." in Path(relative).parts or ":" in relative:
            raise ValueError("JF path must be repository-relative")
        headers = {}
        if os.environ.get("IOMMU_CI_JF_TOKEN"):
            headers["Authorization"] = "Bearer " + os.environ["IOMMU_CI_JF_TOKEN"]
        request = urllib.request.Request(base.rstrip("/") + "/" + urllib.parse.quote(relative, safe="/"), headers=headers)
        cached.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=cached.parent, prefix="download-")
        os.close(fd)
        try:
            import signal
            previous = signal.getsignal(signal.SIGALRM)
            def expired(_signum, _frame):
                raise TimeoutError("asset download exceeded 600 seconds")
            signal.signal(signal.SIGALRM, expired)
            signal.alarm(600)
            try:
                with urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as response, open(temporary, "wb") as output:
                    total = 0
                    while data := response.read(1024 * 1024):
                        total += len(data)
                        if total > spec["size"]:
                            raise ValueError("asset download exceeds locked size")
                        output.write(data)
            finally:
                signal.alarm(0)
                signal.signal(signal.SIGALRM, previous)
            verify(temporary, spec)
            Path(temporary).replace(cached)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"JF download returned HTTP {exc.code}") from None
        except urllib.error.URLError:
            raise RuntimeError("JF download failed; check network and credentials") from None
        finally:
            Path(temporary).unlink(missing_ok=True)
        source, origin = cached, "jf-verified"
    destination = work / "assets" / name / Path(spec["path"]).name
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        shutil.copyfile(source, destination)
    verify(destination, spec)
    write_json(work / "asset-evidence" / (name + ".json"),
               {"name": name, "origin": origin, "spec": spec})
    return destination


def extract(archive, destination):
    # Python's data filter rejects absolute/traversing paths and unsafe links.
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as stream:
        stream.extractall(destination, filter="data")
