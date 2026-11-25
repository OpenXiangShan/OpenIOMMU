# atd_sw_queue_off

Regression test for `fqcsr.fqon` and `cqcsr.cqon`
(`rtl_atd/atd_sw/iommu_atd_fq.sv`, `rtl_atd/atd_sw/iommu_atd_cq.sv`): they may
read 0 (with `busy` 0) only when no fault-record write or command fetch is in
flight and none can start.

The test builds the complete `iommu_wrap` from `iommu_wrap.f` and drives it
only through its public ports. Software clears `fqen` (`cqen`), polls `fqcsr`
(`cqcsr`) until the queue reads off and not busy, and could then reuse the
queue memory. The race lasts one clock cycle, so the race scenarios repeat the
simulation with the disable write moved over a range of cycles (`+delay=<n>`)
and pass only if every run passes.

| Scenario | What it does | master 60238d6 | with the fix |
| --- | --- | --- | --- |
| `fq_disable_race` | a device with an invalid context produces one fault record; memory holds the record's W; `fqen` cleared `delay` cycles after the write is queued (0..79) | FAIL (one delay value) | PASS |
| `cq_disable_race` | data-structure reads take 60 cycles; two `IOTINVAL.VMA` published, `cqen` cleared `delay` cycles after the `cqt` write (0..119) | FAIL (one delay value) | PASS |
| `control_fq_disable_no_hold` | as `fq_disable_race` without the W hold | PASS | PASS |
| `control_fq_disable_after_drain` | software waits for the record before clearing `fqen` | PASS | PASS |
| `control_cq_disable_after_drain` | software waits for cqh before clearing `cqen` | PASS | PASS |

## Running

From the repository root (Verilator 5.x and python3; tested with Verilator
5.052):

    python3 tests/regression/atd_sw_queue_off/run.py                   # CONFIG_RISCV_IOMMU_BOSC_V2_LITE
    python3 tests/regression/atd_sw_queue_off/run.py --config default  # no defines
    # one run of the bench:  <build>/tb_atd_sw_queue_off +scenario=fq_disable_race +delay=35

The runner builds into `tests/regression/atd_sw_queue_off/build/`, prints one
PASS/FAIL line per scenario (with the failing delay values), and exits non-zero
if any scenario fails. Building the complete design takes about 2 minutes in
the lite configuration and about 13 minutes and 30 GB of memory in the default
configuration; the 600 simulations take seconds.
