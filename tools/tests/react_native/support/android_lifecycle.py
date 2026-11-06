"""Strict device-boundary samples for the managed lifecycle runner, never hardware."""

from __future__ import annotations

import base64
import hashlib
import math
import os
import subprocess
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock, patch

from tools.android import state_control
from tools.tests.react_native.support.android_automation import PS, proc_stat
from tools.tests.react_native.support.bleng_ui import ScanDriver
from tools.tests.react_native.test_bleng_android_preparation import PreparationHarness


class LifecycleHarness(PreparationHarness):
    # The inherited SDK harness deliberately accepts multiple WebDriver fakes.
    driver: Any
    replies: Any
    last_phone: Any

    serial = (
        "TESTPHYSICAL42"  # Synthetic; subprocess/network isolated before acquisition.
    )
    activity = "com.blehubblenghost.MainActivity"
    app_path = "/data/app/fixture/com.blehubblenghost/base.apk"

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.source = root / "formal-source.apk"
        with zipfile.ZipFile(self.source, "w") as archive:
            for name, raw in {
                "AndroidManifest.xml": b"fixture manifest",
                "classes.dex": b"dex\n035\0fixture",
                "assets/index.android.bundle": b"fixture bundled JS",
                "lib/arm64-v8a/libbleng_uniffi.so": b"\x7fELFfixture",
                "lib/arm64-v8a/libBleHubReactNativeBleng.so": b"\x7fELFfixture",
            }.items():
                archive.writestr(name, raw)
        self.digest = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.entry = root / "nonexistent-appium.js"
        self.entry.write_text("// isolated process boundary", encoding="utf-8")
        self.node = root / "node"
        self.node.write_text("not executed", encoding="utf-8")
        self.node.chmod(0o700)
        self.driver = cast(Any, ScanDriver("android", []))
        self.driver.app_id = self.driver.foreground = self.app_id
        self.driver.get_settings = Mock(return_value={"waitForIdleTimeout": 1000})
        config = SimpleNamespace(timeout=5.0)
        self.driver.command_executor = SimpleNamespace(
            client_config=config, _client_config=config
        )
        self.driver.close.id = "fixture-close-element"
        self.driver.close.parent = self.driver
        self.driver.close.rect = {"x": 0, "y": 0, "width": 1, "height": 1}
        self.driver.get_window_rect = Mock(return_value=dict(self.driver.close.rect))
        self.driver.get_screenshot_as_png = Mock(
            return_value=base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6Bb0AAAAASUVORK5CYII="
            )
        )
        self.remote_error: BaseException | None = None
        self.quit_error: BaseException | None = None
        self.reject_badging = False
        self.install_error = False
        self.unknown_launch = False
        self.bad_identity = False
        self.upload: str | None = None
        self.app_running = False
        self.last_phone = None
        self.diagnostics_value = "INFO"
        self.diagnostics_restore_error = False
        self.diagnostics_capture_error = False
        self.server_diagnostics_capture_error = False
        self.live_process = None
        self.live_start_error: BaseException | None = None
        self.adb_responses = [
            b"OKAY00040029",
            b"OKAY0006device",
            b"OKAY",
            b"OKAY" + self.boot_id.encode("ascii") + b"\n",
        ]

        def observe_adb(operation: str) -> None:
            if operation == "connect":
                self.sdk.events.append("adb-transport-connect")

        self.adb_after_io = observe_adb

    def __enter__(self):
        super().__enter__()

        def sleep(seconds: float) -> None:
            assert math.isfinite(seconds) and seconds >= 0
            self.clock += seconds

        # UI/helper polling must advance the same clock as SDK commands, even
        # when a missing node or pending state causes no further SDK traffic.
        self.sleep = self.stack.enter_context(patch("time.sleep", side_effect=sleep))
        # The inherited harness's bootstrap lease is local-only and retired before
        # the runner creates/acquires its own actual controller.
        assert (
            self.phone.finish_app_observation(None, finalize_owned=lambda: None) == []
        )
        self.stack.enter_context(patch.object(state_control, "ROOT", self.root))
        self.stack.enter_context(patch("shutil.which", return_value=str(self.node)))
        acquire = state_control.AndroidRadioController.acquire

        def remember(phone):
            self.last_phone = phone
            return acquire(phone)

        self.stack.enter_context(
            patch.object(state_control.AndroidRadioController, "acquire", remember)
        )
        original_remote = self.sdk.remote.side_effect

        def remote(url, **kwargs):
            self.active()
            if self.remote_error is not None:
                raise self.remote_error
            driver = original_remote(url, **kwargs)
            driver.command_executor._client_config = (
                driver.command_executor.client_config
            )
            original_quit = driver.quit.side_effect

            def quit_owned():
                original_quit()
                if self.quit_error is not None:
                    raise self.quit_error
                self.idle()

            driver.quit.side_effect = quit_owned
            return driver

        self.sdk.remote.side_effect = remote
        spawn_appium = self.sdk.spawn.side_effect

        def spawn(command, **kwargs):
            if command[:4] != [str(self.adb), "-s", self.serial, "logcat"]:
                return spawn_appium(command, **kwargs)
            assert command[4:10] == [
                "-b",
                "main,system,crash,events",
                "-v",
                "threadtime",
                "-T",
                "1",
            ]
            assert command[-1] == "*:S" and "-c" not in command
            self.sdk.events.append("live-logcat-start")
            if self.live_start_error is not None:
                raise self.live_start_error
            reader, writer = os.pipe()
            pipe = os.fdopen(reader, "rb", buffering=0)
            self.stack.callback(pipe.close)
            process = Mock(pid=9876, returncode=None, stdout=pipe)
            writer_open = True

            def stop():
                nonlocal writer_open
                if writer_open:
                    os.close(writer)
                    writer_open = False
                process.returncode = -15
                self.sdk.events.append("live-logcat-stop")

            self.stack.callback(stop)
            process.poll.side_effect = lambda: process.returncode
            process.terminate.side_effect = stop
            process.kill.side_effect = stop
            process.wait.side_effect = lambda **_: process.returncode
            os.write(
                writer,
                b"11-06 10:00:00.000 321 322 I ReactNativeJS: fixture live stream\n",
            )
            self.live_process = process
            return process

        self.sdk.spawn.side_effect = spawn
        return self

    def idle(self) -> None:
        super().idle()
        if getattr(self, "app_running", False):
            self.replies[PS] += f"321 10123 {self.app_id}\n"

    def sdk_command(self, command: list[str], **kwargs):
        if command[1:3] == ["dump", "badging"]:
            assert command[-1].endswith("/candidate.apk")
            self.clock += 0.001
            return subprocess.CompletedProcess(
                command,
                int(self.reject_badging),
                f"package: name='{self.app_id}'\nlaunchable-activity: name='{self.activity}'\n",
            )
        if command[1:3] == ["dump", "xmltree"]:
            return subprocess.CompletedProcess(command, 0, "E: manifest (line=1)\n")
        args = tuple(command[3:])
        assert command[:3] == [str(self.adb), "-s", self.serial]
        if args == ("shell", "getprop", "log.tag.BleHubBlengClose"):
            self.replies[args] = self.diagnostics_value + "\n"
        elif args[:3] == ("shell", "setprop", "log.tag.BleHubBlengClose"):
            value = args[3]
            if value == "INFO" and self.diagnostics_restore_error:
                self.replies[args] = "restore rejected\n"
                self.returncodes[args] = 1
            else:
                self.diagnostics_value = "" if value == '""' else value
                self.replies[args] = ""
        elif args == (
            "logcat",
            "-d",
            "-v",
            "threadtime",
            "--pid",
            "3210",
            "-t",
            "1000",
            "appium:I",
            "*:S",
        ):
            if self.server_diagnostics_capture_error:
                self.clock += kwargs["timeout"]
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            self.replies[args] = (
                "11-05 07:00:00.000 3210 3211 I appium: channel read: GET /screenshot\n"
            )
        elif args[:1] == ("logcat",):
            assert args == (
                "logcat",
                "-d",
                "-v",
                "threadtime",
                "--pid",
                "321",
                "-t",
                "1000",
                "BleHubBlengClose:D",
                "ReactNativeJS:V",
                "*:S",
            )
            if self.diagnostics_capture_error:
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            self.replies[args] = (
                "11-05 07:00:00.000 321 322 D BleHubBlengClose: phase=rust.closeResult.return\n"
            )
        elif args[:1] == ("push",):
            assert args[1].endswith("/candidate.apk")
            assert args[2].startswith("/data/local/tmp/blehub-instrumentation-")
            self.upload = args[2]
            self.replies[args] = "uploaded\n"
        elif args[:3] in (("shell", "pm", "install"), ("shell", "su", "-c")):
            assert self.upload is not None and self.upload in args[-1]
            assert "-g" not in " ".join(args).split()
            assert "-t" not in " ".join(args).split()
            self.replies[args] = "rejected\n" if self.install_error else "Success\n"
            self.returncodes[args] = int(self.install_error)
            if not self.install_error:
                self.replies[
                    ("shell", "pm", "list", "packages", "-U", "--user", "0")
                ] += f"package:{self.app_id} uid:10123\n"
        elif args[:3] == ("shell", "rm", "-f"):
            assert args[-1] == self.upload
            self.replies[args] = ""
        elif args == (
            "shell",
            "am",
            "start",
            "-W",
            "--user",
            "0",
            "-n",
            self.app_id + "/" + self.activity,
        ):
            self.app_running = True
            self.replies[PS] += f"321 10123 {self.app_id}\n"
            self.replies[args] = (
                f"Status: ok\nLaunchState: COLD\nActivity: {self.app_id}/.MainActivity\nComplete\n"
            )
            if self.unknown_launch:
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        elif args == ("shell", "am", "force-stop", "--user", "0", self.app_id):
            self.app_running = False
            self.replies[PS] = self.replies[PS].replace(
                f"321 10123 {self.app_id}\n", ""
            )
            self.replies[args] = ""
        elif args[:3] == ("shell", "am", "force-stop"):
            assert args[-1] in ("com.android.packageinstaller", "com.oplus.appdetail")
            self.replies[args] = ""
        self.replies[("shell", "pm", "path", self.app_id)] = (
            "package:" + self.app_path + "\n"
        )
        self.replies[("shell", "sha256sum", self.app_path)] = (
            self.digest + "  " + self.app_path + "\n"
        )
        self.replies[("shell", "pidof", self.app_id)] = (
            "321\n" if self.app_running else ""
        )
        self.replies[("shell", "cat", "/proc/321/stat")] = proc_stat(321)
        self.replies[("shell", "cat", "/proc/321/status")] = (
            "Name:\tapp\nPid:\t321\nUid:\t10123\t10123\t10123\t10123\n"
        )
        self.replies[("shell", "cat", "/proc/321/cmdline")] = self.app_id + "\0"
        if self.bad_identity and args == ("shell", "pidof", self.app_id):
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return super().sdk_command(command, **kwargs)

    def arguments(self) -> dict:
        return {
            "device": self.serial,
            "app_id": self.app_id,
            "activity": self.activity,
            "apk": self.source,
            "apk_sha256": self.digest,
            "appium_home": self.appium_home,
            "appium_entry": self.entry,
            "node": "synthetic-node",
            "expected_helper_sha256": self.expected,
            "appium_port": self.host_port,
            "system_port": self.system_port,
            "output_root": self.root / "runs",
            "timeout": 60.0,
            "phase_timeout": 5.0,
            "cleanup_seconds": 30.0,
            "authorize_install": True,
            "authorize_helper_settings": True,
            "use_root": False,
            "cwd": self.root,
            "run_id": self.run_id,
        }
