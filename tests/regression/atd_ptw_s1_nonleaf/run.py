#!/usr/bin/env python3
"""Regression test for iommu_atd_ptw: fault on first-stage non-leaf PTEs with V=0.

Usage (from anywhere):
    python3 tests/regression/atd_ptw_s1_nonleaf/run.py                   # lite configuration
    python3 tests/regression/atd_ptw_s1_nonleaf/run.py --config default  # no defines
    python3 tests/regression/atd_ptw_s1_nonleaf/run.py --config all      # both
Options: --jobs N, --build-dir DIR, --verilator PATH, -k SCENARIO, --verbose.
Exit status: 0 if every scenario passes, 1 if any fails, 2 if a build fails.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
CONFIGS = {
    "lite": ["+define+CONFIG_RISCV_IOMMU_BOSC_V2_LITE"],
    "default": [],
}
# One entry per testbench (HERE/<top>.sv):
#   sources    None compiles the complete iommu_wrap.f; otherwise the listed
#              files are taken from iommu_wrap.f in the order they appear there
#   scenarios  run as +scenario=<name>
#   sweeps     scenario -> (plusarg, values): one run per value, the scenario
#              passes only if every run passes
BENCHES = {
    'tb_atd_ptw_s1_nonleaf': {
        'sources': None,
        'scenarios': [
            'root_v_cleared',
            'root_v_cleared_pscv',
            'root_v_cleared_cold',
            'root_zeroed_page0_pointer',
            'l1_nonleaf_v_cleared',
            'superpage_v_cleared',
            'control_v_kept',
            'control_leaf_v_cleared',
            'control_root_zeroed',
        ],
    },
}
RUN_TIMEOUT_S = 300
WARNINGS = ["-Wno-fatal", "-Wno-lint", "-Wno-style", "-Wno-TIMESCALEMOD",
            "-Wno-MULTIDRIVEN", "-Wno-PINMISSING"]


def filelist(sources: list[str] | None) -> list[str]:
    if sources is None:
        return ["-F", "iommu_wrap.f"]
    entries = []
    incdirs = []
    for line in (ROOT / "iommu_wrap.f").read_text().splitlines():
        line = line.strip()
        if line.startswith("+incdir+"):
            incdirs.append(line)
        elif line and not line.startswith("//"):
            entries.append(os.path.normpath(line))
    missing = [s for s in sources if os.path.normpath(s) not in entries]
    if missing:
        sys.exit(f"not in iommu_wrap.f: {missing}")
    wanted = {os.path.normpath(s) for s in sources}
    return incdirs + [e for e in entries if e in wanted]


def build(verilator: str, top: str, config: str, build_root: Path, jobs: int) -> Path:
    mdir = build_root / config / top
    mdir.mkdir(parents=True, exist_ok=True)
    tb = os.path.relpath(HERE / f"{top}.sv", ROOT)
    cmd = [verilator, "--binary", "--timing", *WARNINGS, *CONFIGS[config],
           "--top-module", top, *filelist(BENCHES[top]["sources"]), tb,
           "--Mdir", str(mdir), "-j", str(jobs), "-o", top]
    log = mdir / "build.log"
    t0 = time.time()
    print(f"[{config}] building {top} ({' '.join(CONFIGS[config]) or 'no defines'}) ...", flush=True)
    with log.open("w") as f:
        f.write(" ".join(cmd) + "\n")
        f.flush()
        rc = subprocess.run(cmd, cwd=ROOT, stdout=f, stderr=subprocess.STDOUT).returncode
    if rc != 0:
        print(log.read_text()[-4000:])
        print(f"[{config}] build of {top} failed, see {log}")
        sys.exit(2)
    print(f"[{config}] built {top} in {time.time() - t0:.0f} s", flush=True)
    return mdir / top


def run_one(binary: Path, scenario: str, extra: list[str], verbose: bool) -> tuple[bool, str, str]:
    args = [str(binary), f"+scenario={scenario}", *extra] + (["+verbose"] if verbose else [])
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=RUN_TIMEOUT_S,
                           cwd=binary.parent)
        out, rc = p.stdout + p.stderr, p.returncode
    except subprocess.TimeoutExpired:
        out, rc = f"RESULT {scenario} FAIL wall-clock timeout {RUN_TIMEOUT_S} s", -1
    m = None
    for m in re.finditer(rf"^RESULT {re.escape(scenario)} (PASS|FAIL) ?(.*)$", out, re.M):
        pass
    passed = rc == 0 and m is not None and m.group(1) == "PASS"
    detail = m.group(2) if m else f"no result (exit {rc})"
    return passed, detail, out


def run(binary: Path, sweeps: dict, scenario: str, verbose: bool) -> tuple[bool, str, str]:
    if scenario not in sweeps:
        return run_one(binary, scenario, [], verbose)
    arg, values = sweeps[scenario]
    runs = [(v, *run_one(binary, scenario, [f"+{arg}={v}"], verbose)) for v in values]
    failed = [r for r in runs if not r[1]]
    detail = f"{arg}={values[0]}..{values[-1]}: {len(runs) - len(failed)}/{len(runs)} pass"
    if failed:
        detail += (f"; failing {arg}=" + ",".join(str(r[0]) for r in failed) +
                   f"; first failure: {failed[0][2]}")
    return not failed, detail, "\n".join(r[3] for r in runs)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", choices=["lite", "default", "all"], default="lite")
    ap.add_argument("--build-dir", type=Path, default=HERE / "build")
    ap.add_argument("--verilator", default=os.environ.get("VERILATOR", "verilator"))
    ap.add_argument("--jobs", type=int, default=os.cpu_count() or 4)
    ap.add_argument("-k", "--scenario", action="append", help="run only these scenarios")
    ap.add_argument("--verbose", action="store_true", help="print the simulation log of every scenario")
    a = ap.parse_args()
    configs = ["lite", "default"] if a.config == "all" else [a.config]
    failures = 0
    for config in configs:
        for top, bench in BENCHES.items():
            names = [s for s in bench["scenarios"] if not a.scenario or s in a.scenario]
            if not names:
                continue
            binary = build(a.verilator, top, config, a.build_dir.resolve(), a.jobs)
            sweeps = bench.get("sweeps", {})
            t0 = time.time()
            with ThreadPoolExecutor(max(1, min(a.jobs, 8))) as ex:
                results = list(ex.map(lambda s: (s, *run(binary, sweeps, s, a.verbose)), names))
            for name, passed, detail, out in results:
                if a.verbose:
                    print(out)
                print(f"[{config}] {'PASS' if passed else 'FAIL'} {name}: {detail}")
                failures += not passed
            print(f"[{config}] {top}: {sum(r[1] for r in results)}/{len(results)} passed "
                  f"({time.time() - t0:.1f} s)", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
