# atd_ptw_s1_nonleaf

Regression test for the first-stage page-table walker
(`rtl_atd/atd_tw/iommu_atd_ptw.sv`): a non-leaf PTE (or superpage leaf) with
`V=0` must stop the walk with a page fault, whatever its other bits hold.

The test builds the complete `iommu_wrap` from `iommu_wrap.f` and drives it
only through its public ports. Device 3 uses Sv39: IOVA 1 GiB maps through
`root[1] -> l1[0] -> l0[0]` to a data page. The device writes the IOVA once,
software changes one PTE, issues `IOTINVAL.VMA` (GV=0, AV=0) and
`IOFENCE.C(PR=1, PW=1)` and waits for `cqh`, then the device writes the IOVA
again. In the revocation scenarios that write must get an error response and
nothing may be written to the data page after the fence.

| Scenario | Entry changed | master 60238d6 | with the fix |
| --- | --- | --- | --- |
| `root_v_cleared` | `root[1]`: V cleared, PPN kept | FAIL (write lands) | PASS (fault) |
| `root_v_cleared_pscv` | same, `IOTINVAL.VMA` with PSCV=1 | FAIL | PASS |
| `root_v_cleared_cold` | same, no access before the change | FAIL | PASS |
| `root_zeroed_page0_pointer` | `root[1]` = 0 while physical page 0 holds a word that decodes as a valid non-leaf PTE | FAIL | PASS |
| `l1_nonleaf_v_cleared` | `l1[0]` (non-leaf): V cleared | FAIL | PASS |
| `superpage_v_cleared` | `l1[0]` is a 2 MiB leaf: V cleared | FAIL | PASS |
| `control_v_kept` | `root[1]` rewritten with V=1 (write must land) | PASS | PASS |
| `control_leaf_v_cleared` | `l0[0]` (4 KiB leaf): V cleared | PASS | PASS |
| `control_root_zeroed` | `root[1]` = 0, physical page 0 all zero | PASS | PASS |

## Running

From the repository root (Verilator 5.x and python3; tested with Verilator
5.052):

    python3 tests/regression/atd_ptw_s1_nonleaf/run.py                   # CONFIG_RISCV_IOMMU_BOSC_V2_LITE
    python3 tests/regression/atd_ptw_s1_nonleaf/run.py --config default  # no defines

The runner builds into `tests/regression/atd_ptw_s1_nonleaf/build/`, prints one
PASS/FAIL line per scenario with the observed values, and exits non-zero if any
scenario fails. Building the complete design takes about 2 minutes in the lite
configuration and about 13 minutes and 30 GB of memory in the default
configuration.
