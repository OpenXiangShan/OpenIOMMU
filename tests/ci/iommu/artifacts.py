#!/usr/bin/env python3
"""Preserve Unix modes while transferring only reviewable build artifacts."""
import argparse
from pathlib import Path
import tarfile

from assets import extract


def pack(work, output):
    with tarfile.open(output, "w:gz") as stream:
        for name in ("pipeline.json", "bundle", "doctor.json", "logs", "asset-evidence", "stage-errors"):
            if (work / name).exists():
                stream.add(work / name, arcname=name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("pack", "unpack"))
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--archive", required=True, type=Path)
    args = parser.parse_args()
    if args.command == "pack":
        pack(args.work_dir, args.archive)
    else:
        extract(args.archive, args.work_dir)
