#!/usr/bin/env python3
"""Interactive Linux/NVMe acceptance; only actual shell responses count."""
import argparse
import base64
import gzip
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import sys
import time

import pexpect

from common import write_json

FATAL = re.compile(r"Kernel panic|Oops:|BUG:|WARNING: CPU|Queue #\d+ error|"
                   r"dma api failed|Fault |I/O error, dev nvme|Buffer I/O error|"
                   r"Magic mismatch|timeout, disable controller|APB .*timeout|APB .*PSLVERR|"
                   r"Failed to obtain license|probe with driver nvme failed|"
                   r"rcu:.*stall|soft lockup|hard LOCKUP|blocked for more than|hung task|"
                   r"virtio_blk_req_process: unhandled hdr\.type")


class UartLog:
    """Mirror received console chunks to the full artifact and live stdout."""

    def __init__(self, log):
        self.log = log

    def write(self, text):
        self.log.write(text)
        sys.stdout.write(text)

    def flush(self):
        self.log.flush()
        sys.stdout.flush()


def normalize_console(text):
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text.replace("\r", ""))


def qmp_command(qmp_path, execute, arguments=None):
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(30)
        connection.connect(str(qmp_path))
        with connection.makefile("rwb") as stream:
            greeting = json.loads(stream.readline())
            if "QMP" not in greeting:
                raise ValueError("invalid QMP greeting")

            def request(execute, arguments=None):
                stream.write((json.dumps({"execute": execute, "arguments": arguments or {}}) + "\n").encode())
                stream.flush()
                while True:
                    response = json.loads(stream.readline())
                    if "event" in response:
                        continue
                    if "error" in response:
                        raise ValueError("QMP command failed: " + str(response["error"]))
                    return response["return"]
            request("qmp_capabilities")
            return request(execute, arguments)


def read_bypass(qmp_path):
    response = qmp_command(qmp_path, "human-monitor-command", {"command-line": "xp /1wx 0x311f1000"})
    values = re.findall(r":\s+0x([0-9a-fA-F]+)", response)
    if len(values) != 1 or int(values[0], 16) & 1:
        raise ValueError("RTL hardware bypass is enabled or unreadable: " + response)
    return {"address": "0x311f1000", "value": int(values[0], 16), "qmp_response": response}


def check_uart(text):
    match = FATAL.search(text)
    if match:
        raise ValueError("fatal UART signature: " + match.group())


def check_boot_uart(text):
    # The locked d3e51a02 kernel writes the 32-bit BOSC sync pulse with writeq.
    # APB rejects its reserved upper zero word after completing the low word.
    # Account for at most these two boot diagnostics; other APB errors remain
    # fatal, including a nonzero upper word or additional occurrences.
    normalized = normalize_console(text)
    expected = r"(?m)^APB write PSLVERR: addr=0x000002b4 data=0x00000000$"
    records = re.findall(expected, normalized)
    if len(records) > 2:
        raise ValueError("unexpected number of reserved sync-pulse upper-word writes")
    check_uart(re.sub(expected, "", normalized))
    return records


def run(bundle, output, nonce):
    output.mkdir(parents=True, exist_ok=True)
    # Both disks are private copies; neither the bundle nor the downloaded
    # source filesystem is opened writable by QEMU.
    for name in ("rootfs", "nvme"):
        shutil.copyfile(bundle / "rootfs", output / (name + ".img"))
    # Keep the Unix socket path short even when artifact roots are deeply nested.
    import tempfile
    qmp_directory = tempfile.TemporaryDirectory(prefix="iommu-qmp-")
    qmp_path = Path(qmp_directory.name) / "qmp.sock"
    machine = ("qemu_to_iosystem,generated-dtb=on,fw-jump-fdt-addr=0x82200000,"
               "io-system-backend=cmodel,my-virtio-blk=on,dw-pcie=on,"
               "io-system-iommu=rtl,io-system-iommu-placement=external,"
               "io-system-pcie-iommu-map=on,io-system-iommu-picker-out=" + str(bundle / "iommu") +
               ",my-virtio-blk-image=" + str(output / "rootfs.img"))
    argv = [str(bundle / "qemu-system-riscv64"), "-M", machine,
            "-smp", "1", "-m", "8G", "-accel", "tcg,thread=single",
            "-icount", "shift=3,sleep=off",
            "-display", "none", "-serial", "stdio", "-monitor", "none",
            "-qmp", "unix:" + str(qmp_path) + ",server=on,wait=off",
            "-bios", str(bundle / "fw_jump"), "-L", str(bundle / "pc-bios"),
            "-device", "loader,addr=0x311f1000,data=0x80000000,data-len=4",
            "-device", "loader,file=" + str(bundle / "kernel") + ",addr=0x80400000",
            "-drive", "file=" + str(output / "nvme.img") + ",format=raw,if=none,id=nvme0",
            "-device", "nvme,drive=nvme0,serial=openiommu-ci,bus=qti-pcie"]
    write_json(output / "qemu-command.json", {"argv": argv})
    deadline = time.monotonic() + 1100
    uart = output / "uart.log"
    child = None
    try:
        with uart.open("w") as log:
            child = pexpect.spawn(argv[0], argv[1:], cwd=str(output),
                                  encoding="utf-8", codec_errors="replace", echo=False)
            child.logfile_read = UartLog(log)
            prompt = r"\[root@[^\r\n]+\]# "
            child.expect(prompt, timeout=480)
            diagnostics = check_boot_uart(child.before)
            write_json(output / "boot-diagnostics.json", {"reserved_sync_upper_word": diagnostics})
            steps = []

            def command(text, limit=180):
                index = len(steps)
                marker = f"IOMMU_CMD_{nonce}_{index}"
                child.sendline(text + f"; rc=$?; printf '\\n{marker}:%s\\n' \"$rc\"")
                child.expect(r"(?m)^" + marker + r":([0-9]+)\r*$",
                             timeout=min(limit, max(1, deadline - time.monotonic())))
                response = normalize_console(child.before)
                rc = int(child.match.group(1))
                check_uart(response)
                child.expect(prompt, timeout=30)
                check_uart(child.before)
                steps.append({"command": text, "rc": rc, "response": response})
                write_json(output / "guest-evidence.json", {"nonce": nonce, "steps": steps})
                if rc:
                    raise ValueError(f"guest command {index} exited {rc}")
                return response

            command("stty -echo")
            response = command("printf 'SHELL_RESULT:%s\\n' \"$((6 * 7))\"")
            if not re.search(r"(?m)^SHELL_RESULT:42\r?$", response):
                raise ValueError("no real shell arithmetic response")
            response = command("cat /sys/devices/system/cpu/online")
            if response.strip() != "0":
                raise ValueError("expected exactly one online CPU")
            # QEMU reserves a large, mostly zero DT buffer. Compress before
            # UART transfer, preserving the complete original guest bytes.
            response = command("gzip -c /sys/firmware/fdt | base64")
            dt = gzip.decompress(base64.b64decode("".join(response.split()), validate=True))
            if not dt.startswith(bytes.fromhex("d00dfeed")):
                raise ValueError("invalid live DTB")
            (output / "guest.dtb").write_bytes(dt)
            command("test -b /dev/nvme0n1 && test -L /sys/class/nvme/nvme0/device/iommu_group")
            write_json(output / "bypass-register.json", read_bypass(qmp_path))
            command("mkdir -p /mnt/openiommu-ci && mount -t ext4 /dev/nvme0n1 /mnt/openiommu-ci")
            command("ls -la /mnt/openiommu-ci")
            command("printf '%s\\n' '" + nonce + "' > /mnt/openiommu-ci/openiommu-ci.txt")
            command("sync && umount /mnt/openiommu-ci")
            command("mount -t ext4 /dev/nvme0n1 /mnt/openiommu-ci")
            command("test \"$(cat /mnt/openiommu-ci/openiommu-ci.txt)\" = '" + nonce + "'")
            command("sync && umount /mnt/openiommu-ci")
            write_json(output / "bypass-register-after.json", read_bypass(qmp_path))
            # This machine has no guest poweroff device. After every I/O
            # assertion and the final unmount, request normal QEMU shutdown.
            response = qmp_command(qmp_path, "quit")
            write_json(output / "qmp-shutdown.json", {"command": "quit", "response": response})
            child.expect(pexpect.EOF, timeout=90)
            check_uart(child.before)
            child.close()
            if child.exitstatus != 0 or child.signalstatus:
                raise ValueError("QEMU exited abnormally")
        check_boot_uart(uart.read_text(errors="replace"))
        # The last UART chunk may end in a shell prompt without a newline.
        print("\nIOMMU_CI_PASS:qemu-linux-nvme-ext4:" + nonce, flush=True)
    finally:
        if child is not None and child.isalive():
            child.close(force=True)
        if child is not None:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for name in ("rootfs.img", "nvme.img"):
            (output / name).unlink(missing_ok=True)
        qmp_directory.cleanup()


if __name__ == "__main__":
    def cancelled(_signum, _frame):
        raise KeyboardInterrupt("Linux test cancelled")
    signal.signal(signal.SIGTERM, cancelled)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--nonce", required=True)
    args = parser.parse_args()
    run(args.bundle.resolve(), args.output.resolve(), args.nonce)
