"""Failure handling/identity tests; never claim these validate RTL."""
import io
import contextlib
from pathlib import Path
import sys
import tarfile
import tempfile
import time
import unittest

from assets import extract, verify
from build import verify_bundle
from ci import all_cases, check_runtime_output, collect, report, select_cases
from common import run_logged, sha256, write_json
from linux_case import check_boot_uart, check_uart, normalize_console


class HarnessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.work = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        self.pipeline = {"run_id": "current", "source": {"commit": "test"},
                         "dependencies": {}, "selected": ["picker-runtime"], "cases": all_cases()}

    def valid_result(self):
        output = self.work / "results/picker-runtime"
        output.mkdir(parents=True)
        log = output / "driver.log"
        log.write_text("IOMMU_CI_PASS:picker-runtime:nonce\n")
        result = {"id": "picker-runtime", "run_id": "current", "source": self.pipeline["source"],
                  "nonce": "nonce", "status": "PASS", "duration": 1,
                  "evidence": {"driver.log": sha256(log)}}
        write_json(output / "result.json", result)
        return output, result

    def test_documentation_selection(self):
        self.assertEqual(select_cases(all_cases(), ["README.md", "docs/usage.md"]), [])

    def test_unknown_and_missing_diff_run_everything(self):
        for paths in (None, [], ["new-device.sv"], ["README.md", "iommu_wrap.sv"],
                      ["tests/ci/iommu/README.md"], ["LICENSE.sv"]):
            self.assertEqual(select_cases(all_cases(), paths), all_cases())

    def test_force_overrides_documentation(self):
        self.assertEqual(select_cases(all_cases(), ["README.md"], True), all_cases())

    def test_selected_missing_result_fails(self):
        self.assertFalse(report(self.work, self.pipeline))
        self.assertEqual(collect(self.work, self.pipeline)[0]["status"], "FAIL")

    def test_nonselected_is_not_pass(self):
        self.assertTrue(all(r["status"] == "NOT_SELECTED" for r in collect(
            self.work, {**self.pipeline, "selected": []})))

    def test_current_result_passes(self):
        self.valid_result()
        self.assertTrue(report(self.work, self.pipeline))

    def test_stale_run_fails(self):
        output, result = self.valid_result()
        write_json(output / "result.json", {**result, "run_id": "old"})
        self.assertFalse(report(self.work, self.pipeline))

    def test_changed_source_fails(self):
        output, result = self.valid_result()
        write_json(output / "result.json", {**result, "source": {"commit": "old"}})
        self.assertFalse(report(self.work, self.pipeline))

    def test_changed_log_fails(self):
        output, _ = self.valid_result()
        (output / "driver.log").write_text("truncated\n")
        self.assertFalse(report(self.work, self.pipeline))

    def test_echo_cannot_be_pass_marker(self):
        output, result = self.valid_result()
        log = output / "driver.log"
        log.write_text("echo IOMMU_CI_PASS:picker-runtime:nonce\n")
        result["evidence"]["driver.log"] = sha256(log)
        write_json(output / "result.json", result)
        self.assertFalse(report(self.work, self.pipeline))

    def test_stage_error_blocks_passing_results(self):
        self.valid_result()
        write_json(self.work / "stage-errors/build.json", {"error": "missing library"})
        self.assertFalse(report(self.work, self.pipeline))

    def test_bundle_identity_and_hash(self):
        bundle = self.work / "bundle"
        bundle.mkdir()
        (bundle / "library").write_bytes(b"good")
        write_json(bundle / "build.json", {"run_id": "current", "source": self.pipeline["source"],
                                           "dependencies": {}, "files": {"library": sha256(bundle / "library")}})
        self.assertEqual(verify_bundle(self.work, self.pipeline), bundle)
        (bundle / "library").write_bytes(b"changed")
        with self.assertRaises(ValueError):
            verify_bundle(self.work, self.pipeline)

    def test_bad_asset_hash_rejected(self):
        asset = self.work / "asset"
        asset.write_bytes(b"test")
        with self.assertRaises(ValueError):
            verify(asset, {"size": 4, "sha256": "0" * 64})

    def test_archive_traversal_rejected(self):
        archive = self.work / "bad.tar"
        with tarfile.open(archive, "w") as stream:
            entry = tarfile.TarInfo("../escape")
            entry.size = 3
            stream.addfile(entry, io.BytesIO(b"bad"))
        with self.assertRaises(tarfile.FilterError):
            extract(archive, self.work / "output")

    def test_archive_modes_survive_relocation(self):
        archive = self.work / "good.tar"
        with tarfile.open(archive, "w") as stream:
            entry = tarfile.TarInfo("binary")
            entry.size, entry.mode = 4, 0o755
            stream.addfile(entry, io.BytesIO(b"test"))
        extract(archive, self.work / "new-checkout")
        self.assertEqual((self.work / "new-checkout/binary").stat().st_mode & 0o777, 0o755)

    def test_runtime_nonzero_is_failure(self):
        with self.assertRaises(RuntimeError):
            run_logged(["python3", "-c", "raise SystemExit(7)"], self.work / "failed.log")

    def test_live_console_line_endings_preserve_raw_log(self):
        text = "OpenSBI\r\r\nLinux version\r\r\n[root@guest /]# "
        console = io.StringIO()
        log = self.work / "console.log"
        with contextlib.redirect_stdout(console):
            run_logged([sys.executable, "-c", f"print({text!r}, end='', flush=True)"], log)
        self.assertIn("OpenSBI\nLinux version\n[root@guest /]# ", console.getvalue())
        self.assertNotIn("\r", console.getvalue())
        self.assertEqual(log.read_bytes(), text.encode())

    def test_timeout_kills_own_process_group(self):
        script = ("import subprocess,time,pathlib; "
                  "p=subprocess.Popen(['sleep','30']); "
                  "pathlib.Path('child.pid').write_text(str(p.pid)); time.sleep(30)")
        with self.assertRaises(TimeoutError):
            run_logged(["python3", "-c", script], self.work / "timeout.log", cwd=self.work, timeout=0.3)
        pid = int((self.work / "child.pid").read_text())
        status = Path(f"/proc/{pid}/stat")
        deadline = time.monotonic() + 1
        while True:
            try:
                if status.read_text().split()[2] == "Z":
                    break
            except (FileNotFoundError, ProcessLookupError):
                # /proc may report ENOENT or ESRCH when the killed child is
                # reaped between opening and reading its status file.
                break
            if time.monotonic() >= deadline:
                self.fail("timed-out grandchild remains alive")
            time.sleep(0.01)

    def test_license_and_dma_fatals(self):
        for signature in ("Failed to obtain license", "Queue #0 error", "dma api failed",
                          "Kernel panic", "Buffer I/O error", "rcu: INFO: rcu_sched self-detected stall on CPU",
                          "virtio_blk_req_process: unhandled hdr.type=123"):
            with self.assertRaises(ValueError):
                check_uart(signature)

    def test_console_cr_and_ansi_normalization(self):
        self.assertEqual(normalize_console("\x1b[?2004l\r\r\nSHELL_RESULT:42\r\r\n"), "\nSHELL_RESULT:42\n")

    def test_reserved_pulse_diagnostic_is_narrow(self):
        known = "APB write PSLVERR: addr=0x000002b4 data=0x00000000\r\r\n"
        self.assertEqual(len(check_boot_uart(known * 2)), 2)
        for text in (known * 3, known.replace("00000000", "00000001"),
                     known.replace("000002b4", "000002b0")):
            with self.assertRaises(ValueError):
                check_boot_uart(text)
        with self.assertRaises(ValueError):
            check_uart(known)

    def test_linux_driver_uart_diagnostics(self):
        known = "APB write PSLVERR: addr=0x000002b4 data=0x00000000\r\r\n"
        marker = "IOMMU_CI_PASS:qemu-linux-nvme-ext4:nonce\n"
        self.assertEqual(check_runtime_output("qemu-linux-nvme-ext4", known * 2 + marker),
                         [known.strip()] * 2)
        for text in (known * 3, known.replace("00000000", "00000001"),
                     known + "Kernel panic", known + "slv-read timeout"):
            with self.assertRaises(ValueError):
                check_runtime_output("qemu-linux-nvme-ext4", text + marker)

    def test_readonly_apb_rejection_requires_assertion(self):
        rejection = "APB write PSLVERR: addr=0x00000000 data=0xffffffff\n"
        assertion = "EXPECTED_APB_REJECT addr=0x00000000 data=0xffffffff\n"
        self.assertEqual(check_runtime_output("rtl-apb", rejection + assertion), [rejection.strip()])
        for text in (rejection, assertion, rejection * 2 + assertion, rejection + assertion * 2):
            with self.assertRaises(ValueError):
                check_runtime_output("rtl-apb", text)

    def test_expected_apb_rejection_does_not_hide_other_errors(self):
        expected = ("APB write PSLVERR: addr=0x00000000 data=0xffffffff\n"
                    "EXPECTED_APB_REJECT addr=0x00000000 data=0xffffffff\n")
        for extra in ("APB write PSLVERR: addr=0x00000308 data=0x12345678\n",
                      "APB write timeout", "Fault cause=15", "Failed to obtain license"):
            with self.assertRaises(ValueError):
                check_runtime_output("rtl-apb", expected + extra)
        with self.assertRaises(ValueError):
            check_runtime_output("picker-runtime", expected)


if __name__ == "__main__":
    unittest.main()
