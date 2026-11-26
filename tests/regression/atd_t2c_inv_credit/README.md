# atd_t2c_inv_credit

Regression test for the command-queue messages that the ATD sends to the ACD
invalidation engine (`rtl_atd/atd_tc/iommu_atd_t2c.sv`,
`rtl_atd/atd_tc/iommu_atd_c2t.sv`). The engine has 2 entries in the
`CONFIG_RISCV_IOMMU_BOSC_V2_LITE` configuration and 4 otherwise; publishing
more ACD-bound commands than that with one `cqt` write must not drop any of
them.

The test builds the complete `iommu_wrap` with Verilator from `iommu_wrap.f`
and drives it only through its public ports: commands are written into the
command queue in data-structure memory, `cqt` is written and `cqh` polled over
APB. A scenario passes when its `IOFENCE.C` completes (cqh reaches cqt within
3000 cycles), a follow-up `IOFENCE.C` still completes, and `cqcsr` shows no
`cqmf`, `cmd_to` or `cmd_ill`.

| Scenario | What it does | master 60238d6, lite | master 60238d6, default | with the fix |
| --- | --- | --- | --- | --- |
| `inval_burst_fence` | N `IOTINVAL.VMA` + `IOFENCE.C`, one `cqt` write (N = number of engine entries) | FAIL | PASS | PASS |
| `iodir_burst_fence` | two `IODIR.INVAL_DDT` + `IOFENCE.C`, one `cqt` write | FAIL | PASS | PASS |
| `repeated_bursts` | four rounds of `inval_burst_fence` | FAIL | PASS | PASS |
| `bursts_with_page_walks` | eight rounds while a Sv39 device keeps missing in the IOTLB | FAIL | PASS | PASS |
| `linux_probe_sequence` | `IODIR.INVAL_DDT`, `IOTINVAL.VMA`, `IOFENCE.C`, one `cqt` write each, no wait | FAIL | PASS | PASS |
| `control_fence_in_second_write` | N invalidations, then the fence in a later `cqt` write | PASS | PASS | PASS |
| `control_single_inval_fence` | one `IOTINVAL.VMA` + `IOFENCE.C` | PASS | PASS | PASS |

In the default configuration (4 entries) these schedules do not drop a message
on master; there the scenarios check that the credits do not stall the command
queue.

## Running

From the repository root (Verilator 5.x and python3; tested with Verilator
5.052):

    python3 tests/regression/atd_t2c_inv_credit/run.py                   # CONFIG_RISCV_IOMMU_BOSC_V2_LITE
    python3 tests/regression/atd_t2c_inv_credit/run.py --config default  # no defines
    python3 tests/regression/atd_t2c_inv_credit/run.py -k inval_burst_fence --verbose

The runner compiles `-F iommu_wrap.f` plus the testbench into
`tests/regression/atd_t2c_inv_credit/build/`, runs every scenario as
`+scenario=<name>`, prints one PASS/FAIL line per scenario with the observed
values, and exits non-zero if any scenario fails. Building the complete design
takes about 2 minutes in the lite configuration and about 13 minutes and 30 GB
of memory in the default configuration.
