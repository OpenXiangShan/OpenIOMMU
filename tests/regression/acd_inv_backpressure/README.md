# acd_inv_backpressure

Regression test for the ACD invalidation engine
(`rtl_acd/cfg_ctrl_wrap/iommu_acd_inv.sv`): an entry must keep
`inv_tlbcache_req_valid_o` asserted until the TLB cache accepts the request.

The engine is simulated on its own (sources taken from `iommu_wrap.f` in
order) with the number of entries `iommu_wrap` uses (2 with
`CONFIG_RISCV_IOMMU_BOSC_V2_LITE`, 4 otherwise). A TLB-cache model holds
`inv_tlbcache_req_ready_i` low for a while, which is legal back-pressure, and
acknowledges every accepted request one cycle later. A scenario passes when,
observed at the module ports, every message was accepted, every invalidation
reached the TLB cache exactly once with its address, the stalled payload did
not change and every invalidation was acknowledged.

| Scenario | What it does | master 60238d6 | with the fix |
| --- | --- | --- | --- |
| `control_single_ready` | one invalidation, TLB cache ready | PASS | PASS |
| `control_single_stalled` | one invalidation, ready held low for 6 cycles | PASS | PASS |
| `two_gap<G>_stall<S>` (12 cases) | second invalidation G cycles after the first request appears; ready released S cycles later | FAIL | PASS |
| `fill_entries_stall4`, `fill_entries_stall16` | one invalidation per engine entry while ready is low | FAIL | PASS |

## Running

From the repository root (Verilator 5.x and python3; tested with Verilator
5.052):

    python3 tests/regression/acd_inv_backpressure/run.py               # CONFIG_RISCV_IOMMU_BOSC_V2_LITE
    python3 tests/regression/acd_inv_backpressure/run.py --config all  # lite and default

The runner builds into `tests/regression/acd_inv_backpressure/build/`, prints
one PASS/FAIL line per scenario with the observed counts, and exits non-zero if
any scenario fails. Build and run take a few seconds.
