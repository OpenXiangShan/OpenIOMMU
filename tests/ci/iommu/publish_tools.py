#!/usr/bin/env python3
"""Explicit immutable SDK publication. Not invoked by either CI adapter."""
import argparse
import os
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request

from assets import NoRedirect, verify
from common import read_json, write_json


def publish(archive, spec, evidence):
    verify(archive, spec)
    base = os.environ["IOMMU_CI_JF_BASE_URL"]
    token = os.environ["IOMMU_CI_JF_WRITE_TOKEN"]
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.query:
        raise ValueError("invalid JF base URL")
    url = base.rstrip("/") + "/" + urllib.parse.quote(spec["jf_path"], safe="/")
    headers = {"Authorization": "Bearer " + token}
    opener = urllib.request.build_opener(NoRedirect())
    exists = False
    try:
        with opener.open(urllib.request.Request(url, headers=headers, method="HEAD"), timeout=30):
            exists = True
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise RuntimeError(f"JF HEAD returned HTTP {exc.code}") from None
    if not exists:
        # Recheck immutable destination and supply a create-only HTTP condition.
        upload_headers = {**headers, "If-None-Match": "*", "X-Checksum-Sha256": spec["sha256"],
                          "Content-Type": "application/gzip"}
        with opener.open(urllib.request.Request(url, data=Path(archive).read_bytes(),
                                               headers=upload_headers, method="PUT"), timeout=120) as response:
            status = response.status
        if status not in (200, 201):
            raise RuntimeError(f"JF upload returned HTTP {status}")
    # Acceptance is a real download and byte hash, not merely HTTP 201.
    downloaded = Path(evidence).with_suffix(".download.tar.gz")
    try:
        with opener.open(urllib.request.Request(url, headers=headers), timeout=120) as response, downloaded.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
        verify(downloaded, spec)
        write_json(evidence, {"jf_path": spec["jf_path"], "sha256": spec["sha256"],
                              "size": spec["size"], "created": not exists, "download_verified": True})
    finally:
        downloaded.unlink(missing_ok=True)
    print("JF tool package downloaded and verified: " + spec["jf_path"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--spec", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    args = parser.parse_args()
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    publish(args.archive, read_json(args.spec), args.evidence)
