"""Device-free Android capture test boundary helpers and pure samples.

These helpers model the SDK/subprocess boundary only. They never connect to a
phone or execute device commands; callers inject the test case assertions and
capture scope explicitly.
"""

from __future__ import annotations

import hashlib
import os
import shlex
import struct

__all__ = (
    "BOOT",
    "HASH",
    "PCAP",
    "RUN",
    "Device",
    "PipeProcess",
    "ShellFailure",
)

RUN = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
HASH = "4aab8ec95b2421e7915a4a5c1dcaf3d373f7130fb941fc223deb8d90ac562281"
BOOT = "11111111-2222-4333-8444-555555555555"
PCAP = struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 2048, 1)


class PipeProcess:
    """Finite OS pipes exercise the real bounded reader; never starts a process."""

    def __init__(self, output: bytes, code: int = 0, *, stderr: bytes = b""):
        self.stdout = self.pipe(output)
        self.stderr = self.pipe(stderr)
        self.returncode = code
        self.killed = False

    @staticmethod
    def pipe(data):
        read, write = os.pipe()
        # Fixture replies deliberately fit inside a pipe: no feeder/background work.
        assert len(data) < 4096
        os.write(write, data)
        os.close(write)
        return os.fdopen(read, "rb", buffering=0)

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.killed = True


class ShellFailure(RuntimeError):
    """A simulated remote shell exited nonzero after applying partial effects."""


class Device:
    """Strict command interpreter at the SDK/subprocess boundary, not a device."""

    def __init__(self, case):
        self.case = case
        self.commands = []
        self.files = {}
        self.processes = {}
        self.directory = False
        self.directory_inode = 73
        self.boot = BOOT
        self.uid = 0
        self.next_pid = 600
        self.fail = None
        self.ready = True
        self.stubborn = False
        self.drift = None
        self.children = {}
        self.groups = {}
        self.output_fds = {}
        self.cleanup_fault = None
        self.recovery = None
        self.tree_shape = False
        self.stopping = False
        self.stderr = b""
        self.no_children_node = False
        self.binary_nonzero = False
        self.ps_table_override = None

    def stat(self, name):
        data = self.files[name]
        return f"1:80:0:8180:1:{len(data)}:123:123"

    def popen(self, argv, **kwargs):
        self.case.assertEqual(argv[:3], ["/mock/adb", "-s", "synthetic-observer"])
        self.case.assertEqual(argv[3], "shell")
        self.case.assertIn(len(argv), (5, 6))
        if len(argv) == 6:
            self.case.assertEqual(argv[4], "-T")
            root = shlex.split(argv[5])
        else:
            root = shlex.split(argv[4])
        self.case.assertEqual(root[:3], ["su", "--no-pty", "-c"])
        self.case.assertEqual(len(root), 4)
        body = root[3]
        self.case.assertTrue(body.startswith("set -eu;"))
        self.case.assertEqual(kwargs["stdin"], -3)
        self.case.assertFalse(kwargs.get("text", False))
        self.commands.append(body)
        label = body.split("# observer:", 1)[1].splitlines()[0]
        if self.fail == label:
            return PipeProcess(b"injected SDK failure", 1)
        if self.binary_nonzero and label == "cat:capture.pcap":
            return PipeProcess(b"remote binary output", 17)
        try:
            output = self.execute(label, body)
        except ShellFailure as error:
            return PipeProcess(str(error).encode(), 1)
        if self.drift:
            self.drift(label)
        return PipeProcess(output, stderr=self.stderr)

    def execute(self, label, body):
        scope = self.case.scope
        if label == "guard":
            if (
                not self.directory
                or self.boot != scope._server.boot_id
                or (1, self.directory_inode, self.uid, 0x41C0) != scope._remote_pin
            ):
                raise ShellFailure("remote boot/directory assertion failed")
            data = self.files["owner"]
            metadata = self.stat("owner").encode()
            digest = hashlib.sha256(data).hexdigest()
            hashed = f"{digest}  {scope._remote}/owner".encode()
            if "owner" in scope._sources and digest != scope._sources["owner"][1]:
                raise ShellFailure("remote owner assertion failed")
            return b"\n".join([metadata, hashed, data, metadata, hashed, b""])
        if label == "recovery-create":
            self.case.assertIsNone(self.recovery)
            self.recovery = scope._recovery_raw
            self.case.assertIn("set -C", body)
            return b""
        if label == "recovery-check":
            self.case.assertIsNotNone(self.recovery)
            self.case.assertEqual(self.recovery, scope._recovery_raw)
            return f"1:81:0:8180:1:{len(self.recovery)}:123:123\n".encode()
        if label == "recovery-remove":
            self.case.assertFalse(self.directory)
            self.case.assertIsNotNone(self.recovery)
            self.recovery = None
            return b""
        if label == "census":
            # Independent of the tree links: also sees reparented/late processes.
            known_groups = {p["pgid"] for p in scope._processes.values()}
            suspects = []
            for pid in self.processes:
                group = self.groups.get(pid, pid)
                fds = self.output_fds.get(pid, [])
                if group in known_groups or any(
                    f.startswith(scope._remote + "/") for f in fds
                ):
                    suspects.append(f"{pid}:{pid * 10}:{group}")
            return ("\n".join([*suspects, "complete"]) + "\n").encode()
        if label == "prepare":
            if self.directory:
                raise RuntimeError("preexisting remote directory")
            self.directory = True
            self.files["owner"] = scope._nonce.encode()
            return b""
        if label == "directory":
            if not self.directory:
                raise RuntimeError("missing directory")
            return f"1:{self.directory_inode}:{self.uid}:41c0:1:0:123:123\n".encode()
        if label == "boot":
            return (self.boot + "\n").encode()
        if label == "tool":
            return f"{HASH}  /system/bin/tcpdump\n".encode()
        if label.startswith("stat:"):
            name = label[5:]
            return (
                (self.stat(name) if name in self.files else "absent") + "\n"
            ).encode()
        if label.startswith("hash:"):
            name = label[5:]
            digest = hashlib.sha256(self.files[name]).hexdigest()
            return f"{digest}  {scope._remote}/{name}\n".encode()
        if label.startswith("cat:"):
            return self.files[label[4:]]
        if label == "list":
            return ("\n".join(sorted(self.files)) + "\n").encode()
        if label.startswith("launch:"):
            name = label[7:]
            command = scope._collector_argv(name)
            pid = self.next_pid
            self.next_pid += 1
            self.processes[pid] = command
            self.output_fds[pid] = [
                scope._remote
                + "/"
                + ("capture.pcap" if name == "pcap" else "server.log")
            ]
            if self.tree_shape:
                self.processes[pid] = ["/system/bin/sh", "-c", scope._script(name)]
                self.output_fds[pid] = []
            self.files[name + ".pid"] = str(pid).encode()
            self.files[name + ".stderr"] = b""
            if self.ready:
                self.files["capture.pcap" if name == "pcap" else "server.log"] = (
                    PCAP if name == "pcap" else b""
                )
            return str(pid).encode()
        if label.startswith("process:"):
            if self.no_children_node and "/task/" in body:
                raise ShellFailure(
                    "cat: /proc/1/task/1/children: No such file or directory"
                )
            pid = int(label[8:])
            if pid not in self.processes:
                return b"absent\n"
            args = self.processes[pid]
            tail = (
                ["S", "1", str(self.groups.get(pid, pid))]
                + ["0"] * 16
                + [str(pid * 10)]
            )
            parents = {
                child: parent
                for parent, children in self.children.items()
                for child in children
                if child in self.processes
            }
            ps_table = self.ps_table_override or (
                "PID PPID\n"
                + "\n".join(
                    f"{current} {parents.get(current, 1)}"
                    for current in sorted(self.processes)
                )
                + "\n"
            )
            status = f"Pid:\t{pid}\nUid:\t0\t0\t0\t0\n"
            cmdline = b"\0".join(a.encode() for a in args) + b"\0"
            return (
                f"{pid} (collector) {' '.join(tail)}\n--STATUS--\n{status}"
                "--CMDLINE--\n".encode()
                + cmdline
                + b"\n--CHILDREN--\n"
                + ps_table.encode()
                + b"--FD--\n"
                + "\n".join(self.output_fds.get(pid, [])).encode()
                + b"\n\n--ENDSTAT--\n"
                + f"{pid} (collector) {' '.join(tail)}".encode()
                + b"\n--ENDSTATUS--\n"
                + status.encode()
                + b"\n--ENDCMD--\n"
                + cmdline
            )
        if label.startswith("signal:"):
            pid = int(label[7:])
            self.case.assertIn(pid, self.processes)
            self.case.assertNotEqual(pid, self.case.identity.pid)
            self.stopping = True
            if not self.stubborn:
                del self.processes[pid]
            return b""
        if label == "remove":
            self.case.assertIsNotNone(self.recovery)
            if self.cleanup_fault == "partial-rm":
                self.files.pop("owner", None)
                self.files.pop("capture.pcap", None)
                raise ShellFailure("injected partial rm")
            if self.cleanup_fault == "late-file":
                self.files["foreign"] = b"unknown arrival"
                if "ls -A" in body:
                    raise ShellFailure("file set changed at deletion")
            if self.cleanup_fault == "rmdir":
                self.files.clear()
                raise ShellFailure("injected rmdir failure")
            self.files.clear()
            self.directory = False
            return b""
        raise AssertionError(f"unexpected observer command {label}: {body}")
