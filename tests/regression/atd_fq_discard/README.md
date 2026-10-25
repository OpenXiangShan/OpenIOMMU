# atd_fq_discard

Regression test for the fault queue (`rtl_atd/atd_sw/iommu_atd_fq.sv`):
fault records must be discarded while the queue is in error (`fqof` or `fqmf`
set) and while it is disabled.

The test builds the complete `iommu_wrap` from `iommu_wrap.f` and drives it
only through its public ports. Device 2 (Sv39) writes to an unmapped IOVA, so
each write is a page fault; device 3 (Sv39, mapped, needs a page walk) or
device 0 (Bare) then writes once ("probe"). The fault queue is in
data-structure memory, and software does not consume it unless a scenario
says so. A queue with E entries is full after E - 1 records, the next record
sets `fqof`, and 16 more fill the PTW record FIFO; the page fault after that
leaves the walker waiting.

| Scenario | What it does | master 60238d6 | with the fix |
| --- | --- | --- | --- |
| `overflow_2_entry_queue` | 2-entry queue, 18 page faults, Sv39 probe must complete | FAIL (probe never answered) | PASS |
| `overflow_2_entry_queue_bare` | same, probe from a Bare device | FAIL | PASS |
| `overflow_64_entry_queue` | 64-entry queue, 80 page faults, Sv39 probe | FAIL | PASS |
| `record_write_error` | record writes get SLVERR (`fqmf`), 18 faults, probe | FAIL | PASS |
| `overflow_recovery` | 64-entry queue, 80 faults; software sets fqh = fqt and clears `fqof`; no record from the error period may be written afterwards | FAIL | PASS |
| `disable_reenable` | two context faults, the first record's B held; software clears `fqen`, waits for `fqon=0`/`busy=0`, moves `fqb` and sets `fqen`; nothing may reach the new queue | FAIL | PASS |
| `control_one_fault_fewer` | 2-entry queue, 17 faults, probe | PASS | PASS |
| `control_write_error_one_fewer` | SLVERR, 17 faults, probe | PASS | PASS |
| `control_fq_disabled` | queue never enabled, 18 faults, probe | PASS | PASS |
| `control_disable_after_drain` | as `disable_reenable`, software waits for both records first | PASS | PASS |
| `control_disable_no_hold` | as `disable_reenable` without the B hold | PASS | PASS |

## Running

From the repository root (Verilator 5.x and python3; tested with Verilator
5.052):

    python3 tests/regression/atd_fq_discard/run.py                   # CONFIG_RISCV_IOMMU_BOSC_V2_LITE
    python3 tests/regression/atd_fq_discard/run.py --config default  # no defines

The runner builds into `tests/regression/atd_fq_discard/build/`, prints one
PASS/FAIL line per scenario with the observed values, and exits non-zero if any
scenario fails. Building the complete design takes about 2 minutes in the lite
configuration and about 13 minutes and 30 GB of memory in the default
configuration.
