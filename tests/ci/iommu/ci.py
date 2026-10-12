#!/usr/bin/env python3
"""The same CI entrypoint locally, in GitLab and in GitHub."""
import argparse
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET

from build import build, doctor, resolve_dependency, runtime_env, verify_bundle
from common import HERE, SOURCE, read_json, run_logged, sha256, source_identity, vcs_lock, write_json
from linux_case import check_boot_uart, normalize_console


def all_cases():
    cases = read_json(HERE / "cases.json")["cases"]
    if len({case["id"] for case in cases}) != len(cases):
        raise ValueError("duplicate case IDs")
    return cases


def changed_paths(base=None):
    if base is None:
        base = os.environ.get("CI_MERGE_REQUEST_DIFF_BASE_SHA") or os.environ.get("CI_COMMIT_BEFORE_SHA")
    if base is None and os.environ.get("GITHUB_EVENT_PATH"):
        event = read_json(os.environ["GITHUB_EVENT_PATH"])
        base = event.get("pull_request", {}).get("base", {}).get("sha") or event.get("before")
    if not base or not re.fullmatch(r"[0-9a-fA-F]{40}", base) or base == "0" * 40:
        return None
    try:
        raw = subprocess.check_output(["git", "diff", "--no-renames", "--name-only", "-z", base, "HEAD"], cwd=SOURCE)
        return [os.fsdecode(name) for name in raw.split(b"\0") if name]
    except subprocess.CalledProcessError:
        return None


def select_cases(cases, paths=None, force=False):
    # Only positively recognized documentation paths may omit runtime. Unknown
    # paths, missing diffs, renamed RTL and CI changes all run the whole gate.
    def document(path):
        return path in ("README.md", "LICENSE") or path.startswith("docs/") and path.endswith((".md", ".rst", ".txt"))
    if not force and paths and all(document(path) for path in paths):
        return []
    return cases


def select(work, args):
    if (work / "pipeline.json").exists():
        raise ValueError("pipeline exists; use a new work-dir")
    cases = all_cases()
    requested = set(args.case or [])
    if requested - {c["id"] for c in cases}:
        raise ValueError("unknown --case")
    if args.group and args.group not in {c["group"] for c in cases}:
        raise ValueError("unknown --group")
    selected = select_cases(cases, changed_paths(args.base), args.full or bool(requested) or bool(args.group))
    if requested:
        selected = [c for c in selected if c["id"] in requested]
    if args.group:
        selected = [c for c in selected if c["group"] == args.group]
    dependencies = {}
    if selected:
        dependencies["qemu"] = resolve_dependency(read_json(HERE / "dependencies.json")["qemu"])
    pipeline = {"schema": 1, "run_id": uuid.uuid4().hex, "source": source_identity(),
                "selected": [c["id"] for c in selected], "dependencies": dependencies,
                "cases": cases, "created_at": time.time()}
    write_json(work / "pipeline.json", pipeline)
    print("Selected: " + (", ".join(pipeline["selected"]) or "documentation checks only"))
    return pipeline


def check(work, args):
    commands = (
        ("framework", [sys.executable, "-m", "unittest", "discover", "-s", str(HERE),
                       "-p", "test_ci.py", "-v"]),
        ("lint", [sys.executable, str(HERE / "lint_ci.py")]),
        ("diff", ["git", "diff", "--check"]),
    )
    for name, command in commands:
        run_logged(command, work / "logs" / (name + ".log"), cwd=SOURCE, timeout=120)
    return select(work, args)


def pipeline_for(work):
    pipeline = read_json(work / "pipeline.json")
    if pipeline["source"] != source_identity():
        raise ValueError("source changed since selection; use a new work-dir")
    return pipeline


def check_runtime_output(case_id, text):
    diagnostics = []
    if case_id == "qemu-linux-nvme-ext4":
        diagnostics = check_boot_uart(text)
        text = "\n".join(line for line in normalize_console(text).splitlines()
                         if line not in diagnostics)
    lines = text.splitlines()
    if case_id == "rtl-apb":
        expected = "APB write PSLVERR: addr=0x00000000 data=0xffffffff"
        assertion = "EXPECTED_APB_REJECT addr=0x00000000 data=0xffffffff"
        if lines.count(expected) != 1 or lines.count(assertion) != 1:
            raise ValueError("missing or repeated read-only APB rejection/assertion")
        diagnostics.append(expected)
        text = "\n".join(line for line in lines if line != expected)
    if re.search(r"Failed to obtain license|APB .*timeout|APB .*PSLVERR|IOMMU_CI_FAIL|slv-.*timeout", text):
        raise ValueError("fatal runtime signature")
    if case_id != "rtl-dma-fault" and re.search(r"slv-.*fail|Fault ", text):
        raise ValueError("unexpected DMA fault")
    return diagnostics


def execute(work, pipeline, group=None, requested=None):
    if pipeline["source"] != source_identity():
        raise ValueError("source changed before execution; use a new work-dir")
    cases = [c for c in pipeline["cases"] if c["id"] in pipeline["selected"]]
    if group:
        if group not in {c["group"] for c in pipeline["cases"]}:
            raise ValueError("unknown group")
        cases = [c for c in cases if c["group"] == group]
    if requested:
        if set(requested) - set(pipeline["selected"]):
            raise ValueError("requested case is not selected")
        cases = [c for c in cases if c["id"] in requested]
    if not cases:
        report(work, pipeline, group)
        return
    bundle = verify_bundle(work, pipeline)
    env = runtime_env(bundle)
    failed = []
    for case in cases:
        output = work / "results" / case["id"]
        if output.exists():
            raise ValueError("result exists; do not overwrite earlier evidence")
        output.mkdir(parents=True)
        nonce = uuid.uuid4().hex
        command = [str(bundle / "rtl-cases"), case["id"], nonce]
        if case["group"] == "linux":
            command = [sys.executable, str(HERE / "linux_case.py"), "--bundle", str(bundle),
                       "--output", str(output), "--nonce", nonce]
        started = time.monotonic()
        result = {"id": case["id"], "run_id": pipeline["run_id"], "nonce": nonce,
                  "source": pipeline["source"], "status": "FAIL"}
        try:
            with vcs_lock():
                code = run_logged(command, output / "driver.log", cwd=output, env=env,
                                  timeout=case["timeout"], check=False)
            text = (output / "driver.log").read_text(errors="replace")
            marker = "IOMMU_CI_PASS:" + case["id"] + ":" + nonce
            if code or marker not in text.splitlines():
                raise ValueError(f"exit={code}; missing success marker or assertion failed")
            result["expected_diagnostics"] = check_runtime_output(case["id"], text)
            result["status"] = "PASS"
        except Exception as exc:
            result["error"] = str(exc)
            failed.append(case["id"])
        finally:
            result["duration"] = time.monotonic() - started
            result["evidence"] = {str(p.relative_to(output)): sha256(p) for p in output.rglob("*") if p.is_file()}
            write_json(output / "result.json", result)
        print(case["id"] + ": " + result["status"], flush=True)
    report(work, pipeline, group)
    if failed:
        raise RuntimeError("failed cases: " + ", ".join(failed))


def collect(work, pipeline, group=None):
    rows = []
    for case in pipeline["cases"]:
        if group and case["group"] != group:
            continue
        row = {"id": case["id"], "status": "NOT_SELECTED", "duration": 0}
        if case["id"] in pipeline["selected"]:
            row["status"] = "FAIL"
            try:
                output = work / "results" / case["id"]
                result = read_json(output / "result.json")
                if result["id"] != case["id"] or result["run_id"] != pipeline["run_id"] or result["source"] != pipeline["source"]:
                    raise ValueError("stale result identity")
                if not result.get("evidence") or "driver.log" not in result["evidence"]:
                    raise ValueError("missing result evidence")
                for name, digest in result["evidence"].items():
                    path = (output / name).resolve()
                    if not path.is_relative_to(output.resolve()) or sha256(path) != digest:
                        raise ValueError("evidence checksum mismatch")
                if result["status"] not in ("PASS", "FAIL"):
                    raise ValueError("invalid result status")
                if result["status"] == "PASS":
                    expected = "IOMMU_CI_PASS:" + case["id"] + ":" + result["nonce"]
                    if expected not in (output / "driver.log").read_text().splitlines():
                        raise ValueError("success marker missing")
                row = result
            except (OSError, ValueError, KeyError) as exc:
                row["error"] = "missing or invalid result: " + str(exc)
        rows.append(row)
    return rows


def report(work, pipeline=None, group=None):
    errors = []
    if pipeline is None:
        try:
            pipeline = pipeline_for(work)
        except (OSError, ValueError) as exc:
            errors.append(str(exc))
            pipeline = {"run_id": "missing", "source": {}, "selected": [c["id"] for c in all_cases()], "cases": all_cases()}
    rows = collect(work, pipeline, group)
    if not group:
        errors += [str(p.relative_to(work)) for p in (work / "stage-errors").glob("*.json")]
    destination = work / "reports" / (group or "all")
    destination.mkdir(parents=True, exist_ok=True)
    failed = bool(errors) or any(row["status"] == "FAIL" for row in rows)
    write_json(destination / "results.json", {"run_id": pipeline["run_id"], "status": "FAIL" if failed else "PASS",
                                              "stage_errors": errors, "cases": rows})
    lines = ["# OpenIOMMU CI", "", "Run: `" + pipeline["run_id"] + "`", "",
             "| Case | Result |", "| --- | --- |"]
    lines += [f"| {row['id']} | {row['status']} |" for row in rows]
    if errors:
        lines += ["", "Stage errors: " + "; ".join(errors)]
    (destination / "summary.md").write_text("\n".join(lines) + "\n")
    suite = ET.Element("testsuite", name="openiommu", tests=str(len(rows) + len(errors)))
    for row in rows:
        test = ET.SubElement(suite, "testcase", name=row["id"], time=str(row.get("duration", 0)))
        if row["status"] == "NOT_SELECTED":
            ET.SubElement(test, "skipped", message="NOT_SELECTED")
        elif row["status"] != "PASS":
            ET.SubElement(test, "failure", message=row.get("error", "failed"))
    for error in errors:
        ET.SubElement(ET.SubElement(suite, "testcase", name="stage-error"), "failure", message=error)
    ET.ElementTree(suite).write(destination / "junit.xml", encoding="utf-8", xml_declaration=True)
    return not failed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "select", "doctor", "build", "run", "report", "all"))
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--assets-dir", type=Path)
    parser.add_argument("--case", action="append")
    parser.add_argument("--group", choices=("rtl", "linux"))
    parser.add_argument("--base")
    parser.add_argument("--full", action="store_true", default=os.environ.get("IOMMU_CI_FULL") == "1")
    parser.add_argument("--jobs", type=int, default=int(os.environ.get("IOMMU_CI_JOBS", "8")))
    args = parser.parse_args()
    if not args.work_dir:
        if args.command not in ("all", "check", "select"):
            parser.error("--work-dir is required for this stage")
        args.work_dir = SOURCE / ".ci-work" / (time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8])
    work = args.work_dir.resolve()
    work.mkdir(parents=True, exist_ok=True)
    print("WORK_DIR=" + str(work), flush=True)
    def cancelled(_signum, _frame):
        raise KeyboardInterrupt("CI cancelled")
    signal.signal(signal.SIGTERM, cancelled)
    try:
        if args.jobs < 1:
            raise ValueError("--jobs must be positive")
        if args.command == "report":
            return 0 if report(work) else 1
        if args.command == "check":
            check(work, args)
            return 0
        pipeline = select(work, args) if args.command in ("select", "all") else pipeline_for(work)
        if args.command in ("doctor", "all"):
            doctor(work, pipeline)
        if args.command in ("build", "all"):
            build(work, pipeline, args.jobs, args.assets_dir)
        if args.command in ("run", "all"):
            execute(work, pipeline, args.group if args.command == "run" else None, args.case if args.command == "run" else None)
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        write_json(work / "stage-errors" / (args.command + ("-" + args.group if args.group else "") + ".json"),
                   {"stage": args.command, "error": str(exc)})
        print("CI failed: " + str(exc), file=sys.stderr)
        report(work)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
