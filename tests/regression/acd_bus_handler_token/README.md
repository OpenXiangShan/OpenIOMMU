# acd_bus_handler_token

Regression test for the ACD slave interface
(`rtl_acd/bus_handler/iommu_acd_bus_handler_slv_intf.sv`): while every
in-flight token is in use, no request may be transferred to the translation
queue, and a transfer must never carry a token that is still outstanding.

Two testbenches:

- `tb_acd_bus_handler_token.sv` simulates the slave interface alone (sources
  taken from `iommu_wrap.f` in order) with the token width `iommu_wrap` uses
  (256 tokens with `CONFIG_RISCV_IOMMU_BOSC_V2_LITE`, 64 otherwise). The
  translation queue is always ready; tokens are freed only when a scenario
  returns a B response. Checks use the module ports.
- `tb_acd_bus_handler_token_wrap.sv` builds the complete `iommu_wrap` from
  `iommu_wrap.f`. A Bare device issues one write per token while system
  memory withholds every B response, then one more write; that write must not
  reach the memory port before a B response frees a token.

| Scenario | What it does | master 60238d6 | with the fix |
| --- | --- | --- | --- |
| `pool_full_write` | all tokens used, one more AW, one token freed after 16 cycles | FAIL | PASS |
| `pool_full_read` | same with an AR | FAIL | PASS |
| `control_one_token_free` | one token still free when the extra AW arrives | PASS | PASS |
| `wrap_pool_full_write` | complete wrapper, all tokens held by writes without B, one more write | FAIL | PASS |
| `control_wrap_one_token_free` | complete wrapper, one token free | PASS | PASS |

## Running

From the repository root (Verilator 5.x and python3; tested with Verilator
5.052):

    python3 tests/regression/acd_bus_handler_token/run.py                    # CONFIG_RISCV_IOMMU_BOSC_V2_LITE
    python3 tests/regression/acd_bus_handler_token/run.py --config default   # no defines
    python3 tests/regression/acd_bus_handler_token/run.py -k pool_full_write # component only, seconds

The runner builds into `tests/regression/acd_bus_handler_token/build/`, prints
one PASS/FAIL line per scenario with the observed values, and exits non-zero if
any scenario fails. The component testbench builds in seconds; the complete
wrapper takes about 2 minutes (lite) or about 13 minutes and 30 GB of memory
(default).
