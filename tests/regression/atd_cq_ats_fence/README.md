# atd_cq_ats_fence

Regression test for `IOFENCE.C` after `ATS.INVAL`
(`rtl_atd/atd_sw/iommu_atd_cq.sv`, `rtl_atd/atd_wrap/iommu_atd.sv`): the fence
must not complete before the device acknowledges the ATS synchronization, one
fence sends one SYNC request, and fences without a preceding `ATS.INVAL` do
not depend on the device.

The test builds the complete `iommu_wrap` from `iommu_wrap.f` and drives it
only through its public ports. Device 0 has `EN_ATS=1`. A DTI endpoint model
connects, receives the invalidation and SYNC requests and acknowledges them
immediately, after a delay, or only when released.

| Scenario | What it does | master 60238d6 | with the fix |
| --- | --- | --- | --- |
| `ats_inval_ack_withheld` | `ATS.INVAL` + `IOFENCE.C`; acknowledgements withheld for 2000 cycles, then released | FAIL (cqh passes the fence) | PASS |
| `ats_inval_ack_delayed` | acknowledgements 48 cycles after each request; cqh must reach the tail only after the SYNC acknowledgement | FAIL | PASS |
| `one_sync_per_fence` | count SYNC requests for one fence | FAIL (several) | PASS (1) |
| `second_fence_waits_for_its_ack` | a first fence completes; for a second `ATS.INVAL` + fence the acknowledgement is withheld | FAIL | PASS |
| `control_ats_inval_ack_prompt` | acknowledgements sent at once | PASS | PASS |
| `control_fence_without_ats_inval` | `IOTINVAL.VMA` + fence, device never answers | PASS | PASS |
| `control_no_dti_endpoint` | no DTI connection, `IOTINVAL.VMA` + fence | PASS | PASS |

## Running

From the repository root (Verilator 5.x and python3; tested with Verilator
5.052):

    python3 tests/regression/atd_cq_ats_fence/run.py                   # CONFIG_RISCV_IOMMU_BOSC_V2_LITE
    python3 tests/regression/atd_cq_ats_fence/run.py --config default  # no defines

The runner builds into `tests/regression/atd_cq_ats_fence/build/`, prints one
PASS/FAIL line per scenario with the observed values, and exits non-zero if any
scenario fails. Building the complete design takes about 2 minutes in the lite
configuration and about 13 minutes and 30 GB of memory in the default
configuration.
