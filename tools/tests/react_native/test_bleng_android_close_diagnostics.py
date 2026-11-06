"""Synthetic scope tests: actual lease, mocked SDK dispatch and adb resolution."""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from subprocess import Popen
from unittest.mock import patch

from appium.webdriver.webdriver import WebDriver
from appium.webdriver.webelement import WebElement
from selenium.webdriver.remote.client_config import ClientConfig
from selenium.webdriver.remote.command import Command

from tools.android.state_control import AndroidRadioController
from tools.react_native import android_sdk
from tools.react_native.bleng_android_close_diagnostics import AndroidCloseDiagnostics
from tools.react_native.capture_io import publish_new_json

TAG = "log.tag.BleHubBlengClose"
RUN = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
GET = ("shell", "getprop", TAG)
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a5XcAAAAASUVORK5CYII="
)
READS = [
    Command.SCREENSHOT,
    Command.GET_PAGE_SOURCE,
    Command.GET_ELEMENT_RECT,
    Command.GET_WINDOW_RECT,
    Command.GET_ELEMENT_RECT,
]
READ_NAMES = [
    "driver.get_screenshot_as_png",
    "driver.page_source",
    "element.rect.before",
    "driver.get_window_rect",
    "element.rect.after",
]


class FakeExecutor:
    def __init__(self):
        self._client_config = ClientConfig("http://127.0.0.1:4723", timeout=37)

    @property
    def client_config(self):
        return self._client_config


class ReadOnlyDriver:
    """Actual installed SDK accessors; only execute is an isolated SDK boundary."""

    get_window_rect = WebDriver.get_window_rect
    page_source = WebDriver.page_source
    get_screenshot_as_png = WebDriver.get_screenshot_as_png
    get_screenshot_as_base64 = WebDriver.get_screenshot_as_base64

    def __init__(self, test):
        self.test = test
        self.command_executor = FakeExecutor()
        self.session_id = "owned-native-session"
        self.element = WebElement(self, "located-native-close-id")
        self.rect = {"x": -7, "y": -11, "width": 60, "height": 25}
        self.window = {"x": 0, "y": 0, "width": 480, "height": 800}
        self.xml, self.png = '<hierarchy text="本run"/>', PNG
        self.calls, self.timeouts = [], []
        self.hook = None
        self.authority_calls, self.authorized = 0, True
        self.authority_hook = None

    def require_authority(self):
        self.authority_calls += 1
        if self.authority_hook:
            self.authority_hook(self.authority_calls)
        self.test.phone.require_owner()
        if not self.authorized:
            raise RuntimeError("host authority revoked")

    def execute(self, command, params=None):
        # No fallback, device, finder, click, scroll, settings or retry boundary.
        self.test.phone.require_owner()
        self.test.assertTrue(self.authorized)
        index = len(self.calls)
        self.test.assertLess(index, len(READS))
        self.test.assertEqual(command, READS[index])
        if command == Command.GET_ELEMENT_RECT:
            self.test.assertEqual(params, {"id": "located-native-close-id"})
            value = self.rect.copy()
        elif command == Command.GET_WINDOW_RECT:
            self.test.assertIsNone(params)
            value = self.window.copy()
        elif command == Command.GET_PAGE_SOURCE:
            self.test.assertIsNone(params)
            value = self.xml
        elif command == Command.SCREENSHOT:
            self.test.assertIsNone(params)
            value = base64.b64encode(self.png).decode("ascii")
        else:
            self.test.fail(f"unexpected SDK method: {command}")
        self.timeouts.append(self.command_executor.client_config.timeout)
        self.calls.append(command)
        if self.hook:
            value = self.hook(index, value)
        return {"value": value}


class AndroidCloseDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(temporary).resolve() / RUN
        (self.root / "platform").mkdir(parents=True)
        (self.root / "candidate").mkdir()
        self.clock, self.value = 100.0, ""
        self.calls, self.hook = [], None
        self.enterContext(
            patch(
                "tools.react_native.android_sdk.adb_executable",
                return_value=Path("/mock/adb"),
            )
        )
        self.sdk = self.enterContext(
            patch(
                "tools.react_native.android_sdk.run_command", side_effect=self.command
            )
        )
        self.enterContext(
            patch(
                "tools.react_native.bleng_android_close_diagnostics.time.monotonic",
                side_effect=lambda: self.clock,
            )
        )
        self.phone = AndroidRadioController(
            "synthetic-phone",
            RUN,
            self.root / "phone.json",
            lock_root=self.root / "locks",
        )
        self.phone.acquire()
        self.scope = self.new_scope()

    def new_scope(self, **overrides):
        values = {
            "phone": self.phone,
            "run_id": RUN,
            "output": self.root / "platform/close-diagnostics",
            "cwd": self.root,
            "deadline": 120.0,
        }
        return AndroidCloseDiagnostics(**(values | overrides))

    def command(self, argv, *, cwd, timeout, check):
        self.assertEqual(argv[:3], ["/mock/adb", "-s", "synthetic-phone"])
        self.assertEqual(cwd, self.root)
        self.assertTrue(0 < timeout <= 5)
        self.assertFalse(check)
        self.phone.require_owner()
        args = tuple(argv[3:])
        self.calls.append(args)
        if args == GET:
            output = self.value + "\n"
        elif args[:3] == ("shell", "setprop", TAG):
            self.assertTrue(list(self.scope._output.glob("*-intent.json")))
            self.value, output = ("" if args[3] == '""' else args[3]), ""
        elif args[:1] == ("logcat",):
            output = (
                "11-05 10:20:30.123  321  322 D BleHubBlengClose: diagnostic only\n"
            )
        else:
            self.fail(f"unexpected SDK command: {argv}")
        result = subprocess.CompletedProcess(argv, 0, output)
        return self.hook(args, result) if self.hook else result

    def identity(self, **changes):
        path = self.root / "candidate/bound-identity.json"
        path.write_text(
            json.dumps(
                {"serial": "synthetic-phone", "pid": 321, "package": "dev.example.host"}
                | changes
            )
        )
        return path

    def restored(self):
        self.scope.restore(deadline=self.clock + 20)
        self.scope.require_restored()
        self.phone.require_owner()
        self.assertTrue(self.phone.lock.exists())
        self.assertEqual(self.phone.document["actions"], [])

    def preclose_driver(self):
        self.scope.prepare()
        self.identity()
        return ReadOnlyDriver(self)

    def preclose(self, driver, **changes):
        return self.scope.capture_before_close(
            driver,
            driver.element,
            **(
                {"deadline": 120.0, "require_authority": driver.require_authority}
                | changes
            ),
        )

    def live_process(self, script):
        """Replace the actual Popen boundary; never execute the synthetic adb argv."""
        children = []

        def spawn(argv, **kwargs):
            self.phone.require_owner()
            self.assertTrue((self.scope._output / "live-intent.json").exists())
            process = Popen([sys.executable, "-c", script], **kwargs)
            children.append(process)
            return process

        mocked = self.enterContext(
            patch(
                "tools.react_native.bleng_android_close_diagnostics.subprocess.Popen",
                side_effect=spawn,
            )
        )

        def cleanup():
            for process in children:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=2)
                if process.stdout is not None:
                    process.stdout.close()

        self.addCleanup(cleanup)
        return mocked, children

    def live_ready(self, script):
        self.scope.prepare()
        identity = self.identity()
        mocked, children = self.live_process(script)
        self.scope.start_live(identity)
        return mocked, children

    def test_live_fixed_whitelist_provenance_arrivals_and_local_stop_only(self):
        mocked, children = self.live_ready(
            "import os,time; os.write(1,b'original-stream\\x00\\xff'); time.sleep(60)"
        )
        for _ in range(100):
            if self.scope._live_arrivals:
                break
            threading.Event().wait(0.02)
        self.assertTrue(self.scope._live_arrivals)
        self.scope.restore(deadline=130)
        with self.assertRaisesRegex(RuntimeError, "capture"):
            self.scope.require_restored()
        calls = list(self.calls)
        receipt = self.scope.stop_live(130)
        self.scope.require_restored()
        self.assertEqual(self.calls, calls)  # Stop never dispatches device commands.
        self.assertIsNotNone(children[0].poll())
        self.assertFalse(self.scope._live_reader.is_alive())
        args, kwargs = mocked.call_args
        self.assertEqual(
            args[0],
            [
                "/mock/adb",
                "-s",
                "synthetic-phone",
                "logcat",
                "-b",
                "main,system,crash,events",
                "-v",
                "threadtime",
                "-T",
                "1",
                "appium:I",
                "BleHubBlengClose:D",
                "ReactNativeJS:V",
                "ActivityManager:I",
                "ActivityTaskManager:I",
                "AndroidRuntime:E",
                "am_anr:I",
                "am_crash:I",
                "am_proc_died:I",
                "SurfaceMountingManager:V",
                "*:S",
            ],
        )
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        self.assertIs(kwargs["stdout"], subprocess.PIPE)
        self.assertIs(kwargs["stderr"], subprocess.STDOUT)
        self.assertNotIn("shell", kwargs)
        intent = json.loads((self.scope._output / "live-intent.json").read_text())
        self.assertEqual(intent["deadline_monotonic"], 120)
        original = self.root / "candidate/bound-identity.json"
        self.assertEqual(
            Path(intent["identity_raw"]["path"]).read_bytes(), original.read_bytes()
        )
        self.assertEqual(intent["candidate_identity"]["path"], str(original))
        record = json.loads(receipt.read_text())
        self.assertEqual(
            Path(record["raw"]["path"]).read_bytes(), b"original-stream\x00\xff"
        )
        self.assertEqual(record["arrivals"][-1]["offset"], record["raw"]["size"])
        self.assertIn(
            "not request arrival, health or Close proof", record["limitation"]
        )
        self.assertEqual(record["reason"], "stop-requested")
        self.assertIsNone(record["error"])
        self.assertEqual(self.scope.stop_live(130), receipt)
        with self.assertRaisesRegex(RuntimeError, "one-shot"):
            self.scope.start_live(original)
        mocked.assert_called_once()

    def test_live_refuses_missing_identity_wrong_phone_receipt_and_expired_scope(self):
        with patch(
            "tools.react_native.bleng_android_close_diagnostics.subprocess.Popen"
        ) as spawn:
            for mode in (
                "unprepared",
                "missing",
                "wrong-phone",
                "tampered",
                "expired",
                "released",
            ):
                with self.subTest(mode=mode):
                    self.clock = 100
                    self.scope = self.new_scope(output=self.root / "platform" / mode)
                    path = self.identity()
                    if mode != "unprepared":
                        self.scope.prepare()
                    if mode == "missing":
                        path.unlink()
                    elif mode == "wrong-phone":
                        self.identity(serial="foreign")
                    elif mode == "tampered":
                        target = next(self.scope._output.glob("*-command.json"))
                        target.write_bytes(target.read_bytes() + b" ")
                    elif mode == "expired":
                        self.clock = 120
                    elif mode == "released":
                        self.phone.owner = b"not-our-lease"
                    with self.assertRaises(
                        (RuntimeError, ValueError, TimeoutError, FileNotFoundError)
                    ):
                        self.scope.start_live(path)
            spawn.assert_not_called()

    def test_live_silent_stream_is_not_health_and_runtime_is_capped(self):
        self.scope = self.new_scope(output=self.root / "platform/long", deadline=1000)
        _, children = self.live_ready("import time; time.sleep(60)")
        intent = json.loads((self.scope._output / "live-intent.json").read_text())
        self.assertEqual(intent["deadline_monotonic"], 400)
        self.clock = 400
        self.scope._live_reader.join(timeout=2)
        self.assertFalse(self.scope._live_reader.is_alive())
        receipt = self.scope.stop_live(410)
        record = json.loads(receipt.read_text())
        self.assertEqual(record["reason"], "deadline")
        self.assertEqual(record["arrivals"], [])
        self.assertEqual(record["raw"]["size"], 0)
        self.assertIsNotNone(children[0].poll())

    def test_live_byte_limit_and_arrival_metadata_are_bounded(self):
        self.live_ready("import os,time; b=b'x'*65536\nwhile True: os.write(1,b)")
        self.scope._live_reader.join(timeout=5)
        self.assertFalse(self.scope._live_reader.is_alive())
        record = json.loads(self.scope.stop_live(130).read_text())
        self.assertEqual(record["reason"], "byte-limit")
        self.assertEqual(record["raw"]["size"], 8 * 1024 * 1024)
        self.assertLessEqual(len(record["arrivals"]), 64)
        self.assertLess(len(json.dumps(record)), 16000)

    def test_live_early_exit_and_inherited_pipe_do_not_wait_for_eof(self):
        # A descendant retains stdout; the owner must not join on its EOF or kill it.
        self.live_ready(
            "import subprocess,sys; print('before-exit',flush=True); "
            "subprocess.Popen([sys.executable,'-c','import time; time.sleep(2)']); "
            "sys.exit(7)"
        )
        self.scope._live_reader.join(timeout=1.5)
        self.assertFalse(self.scope._live_reader.is_alive())
        with self.assertRaisesRegex(RuntimeError, "exited.*7"):
            self.scope.stop_live(130)
        record = json.loads((self.scope._output / "live-stop.json").read_text())
        self.assertEqual(record["returncode"], 7)
        self.assertIn("before-exit", Path(record["raw"]["path"]).read_text())
        self.assertEqual(record["error"]["type"], "RuntimeError")
        self.assertTrue(self.scope._live_stopped)
        self.restored()
        with self.assertRaisesRegex(RuntimeError, "exited.*7"):
            self.scope.stop_live(
                130
            )  # Capture failure still reaches parent cleanup_errors.

    def test_live_stop_after_authority_loss_still_stops_only_owned_child(self):
        _, children = self.live_ready("import time; time.sleep(60)")
        self.phone.owner = b"not-our-lease"
        with self.assertRaises(RuntimeError):
            self.scope.stop_live(130)
        self.assertIsNotNone(children[0].poll())
        self.assertFalse(self.scope._live_reader.is_alive())
        self.assertTrue((self.scope._output / "live-stop.json").exists())
        self.assertTrue((self.scope._output / "live-stop-failed.json").exists())

    def test_live_stop_kills_only_owned_child_when_terminate_is_ignored(self):
        _, children = self.live_ready(
            "import signal,os,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); "
            "os.write(1,b'ready'); time.sleep(60)"
        )
        for _ in range(100):
            if self.scope._live_arrivals:
                break
            threading.Event().wait(0.02)
        self.assertTrue(self.scope._live_arrivals)
        process = children[0]
        with patch.object(process, "kill", wraps=process.kill) as kill:
            receipt = self.scope.stop_live(130)
        kill.assert_called_once_with()
        self.assertIsNotNone(process.poll())
        record = json.loads(receipt.read_text())
        self.assertEqual(record["reason"], "stop-requested")
        self.assertIsNone(record["error"])

    def test_live_stop_timeout_and_interrupt_are_not_reported_as_restored(self):
        for index, error in enumerate(
            (TimeoutError("bounded join"), KeyboardInterrupt())
        ):
            with self.subTest(error=type(error).__name__):
                self.scope = self.new_scope(
                    output=self.root / "platform" / f"stop-{index}"
                )
                self.live_ready("import time; time.sleep(60)")
                with (
                    patch.object(self.scope._live_reader, "join", side_effect=error),
                    self.assertRaises(type(error)),
                ):
                    self.scope.stop_live(130)
                record = json.loads(
                    (self.scope._output / "live-stop-failed.json").read_text()
                )
                self.assertEqual(record["error"]["type"], type(error).__name__)
                self.scope._live_reader.join(timeout=2)
                self.scope.stop_live(130)
                self.scope.restore(130)
                with self.assertRaises(RuntimeError):
                    self.scope.require_restored()

    def test_live_identity_changed_during_spawn_is_not_rebound(self):
        self.scope.prepare()
        identity = self.identity()
        mocked, children = self.live_process("import time; time.sleep(60)")
        spawn = mocked.side_effect

        def mutate(argv, **kwargs):
            process = spawn(argv, **kwargs)
            identity.write_bytes(identity.read_bytes() + b" ")
            return process

        mocked.side_effect = mutate
        with self.assertRaisesRegex(RuntimeError, "identity changed"):
            self.scope.start_live(identity)
        self.scope._live_reader.join(timeout=2)
        with self.assertRaises(RuntimeError):
            self.scope.stop_live(130)
        self.assertIsNotNone(children[0].poll())
        self.scope.restore(130)
        self.assertEqual(self.value, "")
        with self.assertRaises(RuntimeError):
            self.scope.require_restored()

    def test_live_thread_start_failure_keeps_child_owned_for_bounded_stop(self):
        self.scope.prepare()
        mocked, children = self.live_process("import time; time.sleep(60)")
        with (
            patch(
                "threading.Thread.start",
                side_effect=RuntimeError("thread start failed"),
            ),
            self.assertRaisesRegex(RuntimeError, "thread start failed"),
        ):
            self.scope.start_live(self.identity())
        self.scope.stop_live(130)
        self.assertIsNotNone(children[0].poll())
        self.assertTrue(children[0].stdout.closed)
        mocked.assert_called_once()
        self.restored()
        failed = json.loads((self.scope._output / "live-start-failed.json").read_text())
        self.assertEqual(failed["error"]["message"], "thread start failed")

    def test_live_spawn_failure_without_child_allows_verified_restore_but_keeps_failure(
        self,
    ):
        for index, primary in enumerate(
            (OSError("spawn refused"), KeyboardInterrupt())
        ):
            with self.subTest(error=type(primary).__name__):
                self.scope = self.new_scope(
                    output=self.root / "platform" / f"no-child-{index}"
                )
                self.scope.prepare()
                with (
                    patch(
                        "tools.react_native.bleng_android_close_diagnostics.subprocess.Popen",
                        side_effect=primary,
                    ) as spawn,
                    self.assertRaises(type(primary)) as raised,
                ):
                    self.scope.start_live(self.identity())
                self.assertIs(raised.exception, primary)
                spawn.assert_called_once()
                failed = self.scope._output / "live-start-failed.json"
                original = failed.read_bytes()
                self.assertEqual(
                    json.loads(original)["error"]["type"], type(primary).__name__
                )
                self.assertIsNone(self.scope._live_process)
                stopped = json.loads(self.scope.stop_live(130).read_text())
                self.assertIsNone(stopped["returncode"])
                self.assertTrue(self.scope._live_stopped)
                self.restored()
                self.assertIsNone(self.scope._failure)
                self.assertTrue(
                    json.loads((self.scope._output / "restored.json").read_text())[
                        "verified"
                    ]
                )
                self.assertEqual(failed.read_bytes(), original)

    def test_live_spawn_failure_receipt_tamper_still_prevents_release(self):
        self.scope.prepare()
        with (
            patch(
                "tools.react_native.bleng_android_close_diagnostics.subprocess.Popen",
                side_effect=OSError("spawn refused"),
            ),
            self.assertRaises(OSError),
        ):
            self.scope.start_live(self.identity())
        failed = self.scope._output / "live-start-failed.json"
        failed.write_bytes(failed.read_bytes() + b" ")
        self.scope.stop_live(130)
        self.assertTrue(self.scope._live_stopped)
        self.scope.restore(130)
        self.assertEqual(self.value, "")
        with self.assertRaises(RuntimeError):
            self.scope.require_restored()

    def test_live_spawn_failure_and_keyboard_interrupt_are_preserved(self):
        for index, error in enumerate((OSError("spawn failed"), KeyboardInterrupt())):
            with self.subTest(error=type(error).__name__):
                self.scope = self.new_scope(
                    output=self.root / "platform" / f"spawn-{index}"
                )
                self.scope.prepare()
                with (
                    patch(
                        "tools.react_native.bleng_android_close_diagnostics.subprocess.Popen",
                        side_effect=error,
                    ),
                    self.assertRaises(type(error)),
                ):
                    self.scope.start_live(self.identity())
                record = json.loads(
                    (self.scope._output / "live-start-failed.json").read_text()
                )
                self.assertEqual(record["error"]["type"], type(error).__name__)
                self.scope.restore(130)
                with self.assertRaises(RuntimeError):
                    self.scope.require_restored()

    def test_preclose_saves_original_sdk_reads_same_element_without_verdict(self):
        driver = self.preclose_driver()
        element, config = driver.element, driver.command_executor.client_config
        identity_raw = (self.root / "candidate/bound-identity.json").read_bytes()
        receipt = self.preclose(driver)
        record = json.loads(receipt.read_text())
        self.assertIs(driver.element, element)
        self.assertEqual(driver.calls, READS)
        self.assertEqual(driver.timeouts, [5.0] * 5)
        self.assertEqual(config.timeout, 37)
        self.assertEqual(record["elementId"], element.id)
        self.assertEqual(record["run_id"], RUN)
        self.assertEqual(record["before_rect"], driver.rect)
        self.assertEqual(record["after_rect"], driver.rect)
        self.assertEqual(record["window_rect"], driver.window)
        self.assertIn("not an atomic", record["non_atomic"])
        self.assertIn(
            "PNG -> XML -> element.rect.before -> window rect -> element.rect.after",
            record["non_atomic"],
        )
        self.assertEqual([r["name"] for r in record["reads"]], READ_NAMES)
        self.assertEqual(
            record["candidate_identity_sha256"],
            hashlib.sha256(identity_raw).hexdigest(),
        )
        self.assertEqual((record["serial"], record["pid"]), ("synthetic-phone", 321))
        self.assertNotIn("candidate_identity", record)
        self.assertNotIn("passed", record)
        for kind, raw in (("xml", driver.xml.encode("utf-8")), ("png", driver.png)):
            saved = record["rawfiles"][kind]
            path = Path(saved["path"])
            self.assertEqual(path.read_bytes(), raw)
            self.assertEqual(saved["sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(saved["size"], len(raw))
            self.assertIn(path, self.scope._records)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(record["reads"]), 5)
        self.assertTrue(
            all(
                r["started_monotonic"] <= r["finished_monotonic"]
                for r in record["reads"]
            )
        )
        with self.assertRaisesRegex(RuntimeError, "one-shot"):
            self.preclose(driver)
        self.assertEqual(driver.calls, READS)
        self.restored()

    def test_preclose_sdk_failure_is_not_retried_and_logtag_still_restores(self):
        for index in range(5):
            with self.subTest(index=index):
                self.scope = self.new_scope(
                    output=self.root / "platform" / f"sdk-{index}"
                )
                self.value = "INFO"
                driver = self.preclose_driver()
                error = RuntimeError("SDK read failed")

                def fail(current, value, index=index, error=error):
                    if current == index:
                        raise error
                    return value

                driver.hook = fail
                with self.assertRaises(RuntimeError) as caught:
                    self.preclose(driver)
                self.assertIs(caught.exception, error)
                self.assertEqual(driver.calls, READS[: index + 1])
                self.assertEqual(driver.command_executor.client_config.timeout, 37)
                failed = json.loads(
                    (self.scope._output / "before-close-failed.json").read_text()
                )
                self.assertEqual(failed["error"]["message"], "SDK read failed")
                self.assertFalse((self.scope._output / "before-close.json").exists())
                expected_raws = (
                    set() if index == 0 else {"png"} if index == 1 else {"png", "xml"}
                )
                self.assertEqual(set(failed["rawfiles"]), expected_raws)
                self.assertEqual(
                    [r["name"] for r in failed["reads"]], READ_NAMES[: index + 1]
                )
                for kind in expected_raws:
                    saved = failed["rawfiles"][kind]
                    path = Path(saved["path"])
                    raw = driver.png if kind == "png" else driver.xml.encode("utf-8")
                    self.assertEqual(path.read_bytes(), raw)
                    self.assertEqual(saved["sha256"], hashlib.sha256(raw).hexdigest())
                    self.assertEqual(saved["size"], len(raw))
                    self.assertEqual(
                        self.scope._snapshot(path)[1], self.scope._records[path]
                    )
                with self.assertRaisesRegex(RuntimeError, "one-shot"):
                    self.preclose(driver)
                self.assertEqual(driver.calls, READS[: index + 1])
                self.restored()
                self.assertEqual(self.value, "INFO")

    def test_preclose_first_rect_timeout_pins_prior_raws_and_allows_independent_restore(
        self,
    ):
        for tamper in (None, "png", "xml"):
            with self.subTest(tamper=tamper):
                self.clock, self.value = 100.0, "INFO"
                self.scope = self.new_scope(
                    output=self.root / "platform" / f"rect-timeout-{tamper}"
                )
                driver = self.preclose_driver()
                element, config = driver.element, driver.command_executor.client_config
                primary = TimeoutError("element.rect.before GET timed out")

                def timeout(index, value, primary=primary):
                    if index == 2:
                        self.clock = 120.0
                        raise primary
                    return value

                driver.hook = timeout
                with self.assertRaises(TimeoutError) as caught:
                    self.preclose(driver)
                self.assertIs(caught.exception, primary)
                self.assertIs(driver.element, element)
                self.assertEqual(driver.calls, READS[:3])
                self.assertEqual(config.timeout, 37)
                failed = json.loads(
                    (self.scope._output / "before-close-failed.json").read_text()
                )
                self.assertEqual(failed["elementId"], element.id)
                self.assertEqual([r["name"] for r in failed["reads"]], READ_NAMES[:3])
                self.assertNotIn("before_rect", failed)
                self.assertNotIn("window_rect", failed)
                self.assertNotIn("after_rect", failed)
                self.assertFalse((self.scope._output / "before-close.json").exists())
                self.assertEqual(set(failed["rawfiles"]), {"png", "xml"})
                pins = {}
                for kind, raw in (
                    ("png", driver.png),
                    ("xml", driver.xml.encode("utf-8")),
                ):
                    saved = failed["rawfiles"][kind]
                    path = Path(saved["path"])
                    self.assertEqual(path.read_bytes(), raw)
                    self.assertEqual(saved["sha256"], hashlib.sha256(raw).hexdigest())
                    self.assertEqual(saved["size"], len(raw))
                    pins[path] = self.scope._records[path]
                    self.assertEqual(self.scope._snapshot(path)[1], pins[path])
                with self.assertRaisesRegex(RuntimeError, "one-shot"):
                    self.preclose(driver)
                self.assertEqual(driver.calls, READS[:3])
                if tamper:
                    path = Path(failed["rawfiles"][tamper]["path"])
                    path.write_bytes(path.read_bytes() + b" ")
                self.scope.restore(deadline=140)
                self.assertEqual(self.value, "INFO")
                self.assertTrue(self.scope._restored)
                self.assertEqual({p: self.scope._records[p] for p in pins}, pins)
                if tamper:
                    with self.assertRaises(RuntimeError):
                        self.scope.require_restored()
                else:
                    self.scope.require_restored()
                self.phone.require_owner()

    def test_preclose_host_authority_brackets_every_sdk(self):
        for boundary in range(1, 18):
            with self.subTest(boundary=boundary):
                self.scope = self.new_scope(
                    output=self.root / "platform" / f"authority-{boundary}"
                )
                driver = self.preclose_driver()

                def revoke(count, boundary=boundary, driver=driver):
                    if count == boundary:
                        driver.authorized = False

                driver.authority_hook = revoke
                with self.assertRaisesRegex(RuntimeError, "host authority revoked"):
                    self.preclose(driver)
                # Initial check, then pre/temporary-timeout/post for each read,
                # then pre-publication. No SDK dispatch follows revocation.
                expected = min(5, max(0, (boundary - 2) // 3))
                if boundary >= 4 and (boundary - 4) % 3 == 0:
                    expected += 1
                self.assertEqual(len(driver.calls), min(5, expected))
                self.assertEqual(driver.command_executor.client_config.timeout, 37)
                self.restored()

    def test_preclose_lease_change_before_and_after_each_read_blocks_further_sdk(self):
        for index in range(-1, 5):
            with self.subTest(index=index):
                self.scope = self.new_scope(
                    output=self.root / "platform" / f"lease-{index}"
                )
                driver = self.preclose_driver()
                original = self.phone.owner

                def lose(current, value, index=index):
                    if current == index:
                        self.phone.owner = b"not-our-lease"
                    return value

                driver.hook = lose
                if index == -1:
                    self.phone.owner = b"not-our-lease"
                with self.assertRaises(RuntimeError):
                    self.preclose(driver)
                self.assertEqual(driver.calls, READS[: index + 1])
                self.assertEqual(driver.command_executor.client_config.timeout, 37)
                self.phone.owner = original
                self.restored()

    def test_preclose_deadline_shrinks_http_timeout_without_extending_scenario(self):
        driver = self.preclose_driver()

        def advance(index, value):
            self.clock += 0.5
            return value

        driver.hook = advance
        self.preclose(driver, deadline=104.5)
        self.assertEqual(driver.timeouts, [4.5, 4.0, 3.5, 3.0, 2.5])
        self.assertEqual(self.scope._deadline, 120)
        self.assertEqual(driver.command_executor.client_config.timeout, 37)
        self.restored()

    def test_preclose_authority_checks_consume_the_same_http_deadline(self):
        driver = self.preclose_driver()

        def check(count):
            if count == 3:  # Last authority check immediately before the first SDK.
                self.clock = 102.0

        driver.authority_hook = check
        self.preclose(driver, deadline=104.0)
        self.assertEqual(driver.timeouts, [2.0] * 5)
        self.assertEqual(driver.command_executor.client_config.timeout, 37)
        self.restored()

    def test_preclose_pre_dispatch_scope_or_config_changes_block_sdk(self):
        for mode in ("lease", "timeout", "config", "session", "element-id"):
            with self.subTest(mode=mode):
                self.scope = self.new_scope(
                    output=self.root / "platform" / f"pre-{mode}"
                )
                driver = self.preclose_driver()
                config, owner = driver.command_executor.client_config, self.phone.owner
                foreign = ClientConfig("http://127.0.0.1:9999", timeout=91)

                def change(
                    count, mode=mode, driver=driver, config=config, foreign=foreign
                ):
                    if count != 3:
                        return
                    if mode == "lease":
                        self.phone.owner = b"not-our-lease"
                    elif mode == "timeout":
                        config.timeout = 88
                    elif mode == "config":
                        driver.command_executor._client_config = foreign
                    elif mode == "session":
                        driver.session_id = "foreign-session"
                    else:
                        driver.element._id = "foreign-element"

                driver.authority_hook = change
                with self.assertRaises(RuntimeError):
                    self.preclose(driver)
                self.assertEqual(driver.calls, [])
                self.assertEqual(config.timeout, 37)
                self.assertEqual(foreign.timeout, 91)
                self.phone.owner = owner
                self.restored()

    def test_preclose_sdk_failure_preserves_primary_and_post_authority_failure(self):
        driver = self.preclose_driver()
        primary = OSError("SDK primary error")

        def fail(index, value):
            driver.authorized = False
            raise primary

        driver.hook = fail
        with self.assertRaises(OSError) as caught:
            self.preclose(driver)
        self.assertIs(caught.exception, primary)
        self.assertTrue(any("authority revoked" in note for note in primary.__notes__))
        self.assertEqual(driver.command_executor.client_config.timeout, 37)
        self.assertEqual(driver.calls, READS[:1])
        self.restored()

    def test_preclose_deadline_and_sdk_identity_timeout_drift_fail_closed(self):
        for mode in (
            "expired",
            "extended",
            "post-deadline",
            "executor",
            "config",
            "timeout",
            "session",
            "element-id",
            "element-parent",
        ):
            with self.subTest(mode=mode):
                self.clock = 100
                self.scope = self.new_scope(output=self.root / "platform" / mode)
                driver = self.preclose_driver()
                executor = driver.command_executor
                config = executor.client_config
                foreign = ClientConfig("http://127.0.0.1:9999", timeout=91)

                def drift(
                    index,
                    value,
                    mode=mode,
                    driver=driver,
                    executor=executor,
                    config=config,
                    foreign=foreign,
                ):
                    if mode == "post-deadline":
                        self.clock = 120
                    elif mode == "executor":
                        driver.command_executor = FakeExecutor()
                    elif mode == "config":
                        executor._client_config = foreign
                    elif mode == "timeout":
                        config.timeout = 99
                    elif mode == "session":
                        driver.session_id = "foreign-session"
                    elif mode == "element-id":
                        driver.element._id = "different-id"
                    elif mode == "element-parent":
                        driver.element._parent = object()
                    return value

                driver.hook = drift
                deadline = {"expired": 100, "extended": 121}.get(mode, 120)
                with self.assertRaises((TimeoutError, RuntimeError)):
                    self.preclose(driver, deadline=deadline)
                self.assertEqual(
                    len(driver.calls), 0 if mode in {"expired", "extended"} else 1
                )
                self.assertEqual(config.timeout, 37)
                self.assertEqual(foreign.timeout, 91)
                if mode == "executor":
                    self.assertEqual(driver.command_executor.client_config.timeout, 37)
                self.restored()

    def test_preclose_empty_id_blocks_raw_capture_and_invalid_rect_preserves_raws(self):
        cases = [
            None,
            "",
            " ",
            {"x": float("nan")},
            {"y": float("inf")},
            {"width": 0},
            {"height": -1},
            {"width": True},
        ]
        for index, change in enumerate(cases):
            with self.subTest(change=change):
                self.scope = self.new_scope(
                    output=self.root / "platform" / f"rect-{index}"
                )
                driver = self.preclose_driver()
                if isinstance(change, dict):
                    driver.rect.update(change)
                else:
                    driver.element._id = change
                with self.assertRaises(ValueError):
                    self.preclose(driver)
                self.assertEqual(
                    len(driver.calls), 3 if isinstance(change, dict) else 0
                )
                failed = json.loads(
                    (self.scope._output / "before-close-failed.json").read_text()
                )
                self.assertEqual(
                    set(failed["rawfiles"]),
                    {"png", "xml"} if isinstance(change, dict) else set(),
                )
                self.assertEqual(driver.command_executor.client_config.timeout, 37)
                self.restored()

    def test_preclose_rect_drift_retains_raws_and_fails_without_relocation(self):
        driver = self.preclose_driver()

        def drift(index, value):
            return value | {"x": value["x"] + 1} if index == 4 else value

        driver.hook = drift
        with self.assertRaisesRegex(RuntimeError, "rect changed"):
            self.preclose(driver)
        self.assertEqual(driver.calls, READS)
        failed = json.loads(
            (self.scope._output / "before-close-failed.json").read_text()
        )
        self.assertNotEqual(failed["before_rect"], failed["after_rect"])
        self.assertEqual(set(failed["rawfiles"]), {"xml", "png"})
        self.restored()

    def test_preclose_invalid_or_oversized_raws_and_exact_limits(self):
        for mode in (
            "xml-big",
            "xml-utf8-big",
            "xml-type",
            "png-big",
            "non-png",
            "limits",
        ):
            with self.subTest(mode=mode):
                self.scope = self.new_scope(output=self.root / "platform" / mode)
                driver = self.preclose_driver()
                if mode == "xml-big":
                    driver.xml = "x" * (1024 * 1024 + 1)
                elif mode == "xml-utf8-big":
                    driver.xml = "界" * (1024 * 1024 // 3 + 1)
                elif mode == "xml-type":
                    driver.xml = b"not-text"
                elif mode == "png-big":
                    driver.png = PNG + b"x" * (8 * 1024 * 1024)
                elif mode == "non-png":
                    driver.png = b"not-PNG"
                else:
                    driver.xml = "x" * (1024 * 1024)
                    driver.png = PNG + b"x" * (8 * 1024 * 1024 - len(PNG))
                if mode == "limits":
                    self.preclose(driver)
                else:
                    with self.assertRaises(ValueError):
                        self.preclose(driver)
                    failed = json.loads(
                        (self.scope._output / "before-close-failed.json").read_text()
                    )
                    self.assertEqual(
                        set(failed["rawfiles"]),
                        {"png"} if mode.startswith("xml-") else set(),
                    )
                    self.assertEqual(
                        driver.calls,
                        READS[:2] if mode.startswith("xml-") else READS[:1],
                    )
                self.assertEqual(driver.command_executor.client_config.timeout, 37)
                self.restored()

    def test_preclose_candidate_and_existing_receipt_tampering(self):
        for mode in (
            "candidate-before",
            "candidate-during",
            "receipt-before",
            "receipt-during",
        ):
            with self.subTest(mode=mode):
                self.scope = self.new_scope(output=self.root / "platform" / mode)
                driver = self.preclose_driver()
                candidate = self.root / "candidate/bound-identity.json"
                self.scope.capture(candidate, deadline=120)
                target = (
                    candidate
                    if mode.startswith("candidate")
                    else next(self.scope._output.glob("*-command.json"))
                )
                if mode.endswith("before"):
                    target.write_bytes(target.read_bytes() + b" ")
                else:

                    def tamper(index, value, target=target):
                        target.write_bytes(target.read_bytes() + b" ")
                        return value

                    driver.hook = tamper
                with self.assertRaises(RuntimeError):
                    self.preclose(driver)
                self.assertEqual(len(driver.calls), 0 if mode.endswith("before") else 1)
                self.assertEqual(driver.command_executor.client_config.timeout, 37)
                self.scope.restore(deadline=130)
                self.assertEqual(self.value, "")
                if mode.startswith("receipt"):
                    with self.assertRaises(RuntimeError):
                        self.scope.require_restored()
                else:
                    self.scope.require_restored()

    def test_preclose_published_raws_and_json_reject_drift_links_and_replacement(self):
        for kind, mutation in (
            ("xml", "content"),
            ("png", "hardlink"),
            ("png", "symlink"),
            ("json", "replacement"),
        ):
            with self.subTest(kind=kind, mutation=mutation):
                self.scope = self.new_scope(
                    output=self.root / "platform" / f"{kind}-{mutation}"
                )
                driver = self.preclose_driver()
                self.preclose(driver)
                path = self.scope._output / f"before-close.{kind}"
                linked = self.root / f"{kind}-{mutation}.raw"
                if mutation == "content":
                    path.write_bytes(path.read_bytes() + b" ")
                elif mutation == "hardlink":
                    linked.hardlink_to(path)
                elif mutation == "symlink":
                    path.rename(linked)
                    path.symlink_to(linked)
                else:
                    linked.write_bytes(path.read_bytes())
                    linked.replace(path)
                with self.assertRaises(RuntimeError):
                    self.scope.capture(
                        self.root / "candidate/bound-identity.json", deadline=120
                    )
                self.scope.restore(deadline=130)
                self.assertEqual(self.value, "")
                with self.assertRaises(RuntimeError):
                    self.scope.require_restored()

    def test_preclose_raw_publication_is_exclusive_and_failure_not_swallowed(self):
        for kind in ("png", "xml"):
            with self.subTest(kind=kind):
                self.scope = self.new_scope(
                    output=self.root / "platform" / f"exclusive-{kind}"
                )
                driver = self.preclose_driver()
                path = self.scope._output / f"before-close.{kind}"
                path.write_bytes(b"unowned-existing-evidence")
                with self.assertRaises(FileExistsError):
                    self.preclose(driver)
                self.assertEqual(path.read_bytes(), b"unowned-existing-evidence")
                failed = json.loads(
                    (self.scope._output / "before-close-failed.json").read_text()
                )
                self.assertEqual(
                    set(failed["rawfiles"]), {"png"} if kind == "xml" else set()
                )
                if kind == "xml":
                    self.assertEqual(
                        (self.scope._output / "before-close.png").read_bytes(), PNG
                    )
                else:
                    self.assertFalse((self.scope._output / "before-close.xml").exists())
                self.assertEqual(driver.command_executor.client_config.timeout, 37)
                self.scope.restore(deadline=130)
                self.assertEqual(self.value, "")
                with self.assertRaises(RuntimeError):
                    self.scope.require_restored()

    def test_preclose_requires_host_guard_and_prepared_scope(self):
        self.identity()
        driver = ReadOnlyDriver(self)
        with self.assertRaisesRegex(RuntimeError, "prepared"):
            self.preclose(driver)
        self.assertEqual(driver.calls, [])
        self.scope = self.new_scope(output=self.root / "platform" / "guard-missing")
        self.scope.prepare()
        with self.assertRaises(TypeError):
            self.preclose(driver, require_authority=None)
        self.assertEqual(driver.calls, [])
        self.restored()

    def test_round_trip_empty_info_debug_and_immutable_command_receipts(self):
        for original in ("", "INFO", "DEBUG"):
            with self.subTest(original=original):
                self.scope = self.new_scope(
                    output=self.root / "platform" / ("scope-" + (original or "empty"))
                )
                self.value = original
                self.scope.prepare()
                self.assertEqual(self.value, "DEBUG")
                self.restored()
                self.assertEqual(self.value, original)
                records = [
                    json.loads(p.read_text())
                    for p in self.scope._output.glob("*-command.json")
                ]
                self.assertEqual(len(records), 6)
                self.assertTrue(
                    all(
                        r["error"] is None
                        and r["run_id"] == RUN
                        and "stdout" in r
                        and "argv" in r
                        for r in records
                    )
                )

    def test_construction_is_local_and_rejects_wrong_controller_or_run(self):
        self.sdk.assert_not_called()
        for changes in ({"phone": object()}, {"run_id": "other"}):
            with (
                self.subTest(changes=changes),
                self.assertRaises((ValueError, TypeError)),
            ):
                self.new_scope(**changes)
        self.sdk.assert_not_called()

    def test_prepare_and_restore_are_one_shot(self):
        self.scope.prepare()
        before = len(self.calls)
        with self.assertRaises(RuntimeError):
            self.scope.prepare()
        self.assertEqual(len(self.calls), before)
        self.restored()
        before = len(self.calls)
        self.restored()  # Revalidation, never another SDK dispatch.
        self.assertEqual(len(self.calls), before)

    def test_unknown_original_and_current_are_never_overwritten(self):
        self.value = "unknown"
        with self.assertRaises(ValueError):
            self.scope.prepare()
        with self.assertRaises((RuntimeError, ValueError)):
            self.scope.restore(deadline=130)
        self.assertTrue(all(c == GET for c in self.calls))

    def test_drift_to_other_known_level_is_not_ours(self):
        self.scope.prepare()
        self.value = "WARN"
        before = len(self.calls)
        with self.assertRaises(RuntimeError):
            self.scope.restore(deadline=130)
        self.assertEqual(self.calls[before:], [GET])
        self.assertEqual(self.value, "WARN")
        with self.assertRaises(RuntimeError):
            self.scope.require_restored()

    def test_prepare_timeout_after_mutation_can_rollback_but_never_pass(self):
        def timeout(args, result):
            if args == ("shell", "setprop", TAG, "DEBUG"):
                raise android_sdk.AndroidSdkError(
                    "transport timeout"
                ) from subprocess.TimeoutExpired(
                    result.args, 5, output="partial output"
                )
            return result

        self.hook = timeout
        with self.assertRaisesRegex(android_sdk.AndroidSdkError, "transport timeout"):
            self.scope.prepare()
        self.assertEqual(self.value, "DEBUG")
        self.hook = None
        self.scope.restore(deadline=130)
        self.assertEqual(self.value, "")
        with self.assertRaises(RuntimeError):
            self.scope.require_restored()
        failed = [
            json.loads(p.read_text()) for p in self.scope._output.glob("*-command.json")
        ]
        self.assertTrue(
            any(r["error"] and r["stdout"] == "partial output" for r in failed)
        )

    def test_post_command_deadline_and_cleanup_deadline_are_distinct(self):
        def expire(args, result):
            if args[:2] == ("shell", "setprop"):
                self.clock = 121
            return result

        self.hook = expire
        with self.assertRaises(TimeoutError):
            self.scope.prepare()
        self.hook = None
        self.scope.restore(deadline=140)
        self.assertEqual(self.value, "")
        with self.assertRaises(RuntimeError):
            self.scope.require_restored()

    def test_expired_restore_does_not_mean_restored(self):
        self.scope.prepare()
        with self.assertRaises(TimeoutError):
            self.scope.restore(deadline=100)
        with self.assertRaises(RuntimeError):
            self.scope.require_restored()
        self.assertEqual(self.value, "DEBUG")

    def test_lease_run_actions_and_phone_changes_block_sdk(self):
        self.scope.prepare()
        before = len(self.calls)
        cases = (
            (self.phone, "run_id", "other"),
            (self.phone, "device", "other"),
            (self.phone, "owner", b"changed"),
        )
        for target, key, value in cases:
            old = getattr(target, key)
            setattr(target, key, value)
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                self.scope.capture(self.identity(), deadline=120)
            setattr(target, key, old)
        self.phone.document["actions"].append({"action": "off"})
        with self.assertRaises(RuntimeError):
            self.scope.capture(self.identity(), deadline=120)
        self.phone.document["actions"].clear()
        self.assertEqual(len(self.calls), before)
        self.restored()

    def test_capture_original_pid_fixed_tags_no_verdict_and_bound_output(self):
        self.scope.prepare()
        receipt = self.scope.capture(self.identity(), deadline=120)
        self.assertEqual(
            self.calls[-1],
            (
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
            ),
        )
        record = json.loads(receipt.read_text())
        self.assertIn("11-05 10:20:30.123", record["stdout"])
        self.assertNotIn("close_success", record)
        self.hook = lambda args, result: subprocess.CompletedProcess(
            result.args, 0, "x" * (2 * 1024 * 1024 + 1)
        )
        with self.assertRaises(RuntimeError):
            self.scope.capture(self.identity(), deadline=120)
        self.hook = None
        records = [
            json.loads(p.read_text()) for p in self.scope._output.glob("*-command.json")
        ]
        self.assertTrue(
            all(len(r["stdout"].encode()) <= 2 * 1024 * 1024 for r in records)
        )
        self.restored()

    def test_capture_wrong_identity_or_pid_refused_without_sdk(self):
        self.scope.prepare()
        before = len(self.calls)
        for changes in ({"pid": True}, {"pid": 0}, {"pid": "321"}, {"serial": "other"}):
            with (
                self.subTest(changes=changes),
                self.assertRaises((RuntimeError, ValueError)),
            ):
                self.scope.capture(self.identity(**changes), deadline=120)
        wrong = self.root / "wrong.json"
        wrong.write_bytes(self.identity().read_bytes())
        with self.assertRaises(ValueError):
            self.scope.capture(wrong, deadline=120)
        self.assertEqual(len(self.calls), before)
        self.restored()

    def test_capture_identity_changed_during_sdk_preserves_error_and_restore(self):
        self.scope.prepare()
        identity = self.identity()

        def change(args, result):
            if args[0] == "logcat":
                identity.write_bytes(identity.read_bytes() + b" ")
            return result

        self.hook = change
        with self.assertRaises(RuntimeError):
            self.scope.capture(identity, deadline=120)
        self.hook = None
        self.restored()
        self.assertTrue(
            any(
                json.loads(p.read_text())["error"]
                for p in self.scope._output.glob("*-command.json")
            )
        )

    def test_receipt_tampering_still_allows_rollback_not_success(self):
        self.scope.prepare()
        receipt = next(self.scope._output.glob("*-command.json"))
        receipt.write_bytes(receipt.read_bytes() + b" ")
        with self.assertRaises(RuntimeError):
            self.scope.capture(self.identity(), deadline=120)
        self.scope.restore(deadline=130)
        self.assertEqual(self.value, "")
        with self.assertRaises(RuntimeError):
            self.scope.require_restored()

    def test_terminal_receipt_replacement_is_rejected(self):
        self.scope.prepare()
        self.restored()
        terminal = self.scope._output / "restored.json"
        raw = terminal.read_bytes()
        replacement = terminal.with_suffix(".replacement")
        replacement.write_bytes(raw)
        replacement.replace(terminal)
        with self.assertRaises(RuntimeError):
            self.scope.require_restored()

    def test_symlink_parent_identity_and_existing_output_refused(self):
        with self.assertRaises(FileExistsError):
            self.new_scope()
        linked = self.root / "linked"
        linked.symlink_to(self.root / "platform", target_is_directory=True)
        with self.assertRaises((RuntimeError, ValueError)):
            self.new_scope(output=linked / "diagnostics")
        self.scope.prepare()
        identity = self.identity()
        raw = identity.read_bytes()
        identity.unlink()
        target = self.root / "target.json"
        target.write_bytes(raw)
        identity.symlink_to(target)
        with self.assertRaises(RuntimeError):
            self.scope.capture(identity, deadline=120)
        self.restored()

    def test_restore_timeout_after_write_keeps_unknown_completion(self):
        self.scope.prepare()

        def timeout(args, result):
            if args[:2] == ("shell", "setprop"):
                raise subprocess.TimeoutExpired(
                    result.args, 5, output=b"cleanup partial"
                )
            return result

        self.hook = timeout
        with self.assertRaises(subprocess.TimeoutExpired):
            self.scope.restore(deadline=130)
        self.assertEqual(self.value, "")
        self.hook = None
        with self.assertRaises(RuntimeError):
            self.scope.require_restored()
        before = len(self.calls)
        with self.assertRaises(RuntimeError):
            self.scope.restore(deadline=140)
        self.assertEqual(len(self.calls), before)
        records = [
            json.loads(p.read_text()) for p in self.scope._output.glob("*-command.json")
        ]
        self.assertTrue(
            any(r["error"] and r["stdout"] == "cleanup partial" for r in records)
        )

    def test_nonzero_prepare_write_and_readback_mismatch_stay_failed(self):
        for mode in ("nonzero", "readback"):
            with self.subTest(mode=mode):
                self.scope = self.new_scope(output=self.root / "platform" / mode)
                self.value = "INFO"

                def fail(args, result, mode=mode):
                    if args[:2] == ("shell", "setprop") and args[3] == "DEBUG":
                        if mode == "nonzero":
                            return subprocess.CompletedProcess(
                                result.args, 1, "write failed\n"
                            )
                        self.value = "INFO"
                    return result

                self.hook = fail
                with self.assertRaises(RuntimeError):
                    self.scope.prepare()
                self.hook = None
                self.scope.restore(deadline=130)
                self.assertEqual(self.value, "INFO")
                with self.assertRaises(RuntimeError):
                    self.scope.require_restored()

    def test_capture_does_not_refresh_the_original_pid(self):
        self.scope.prepare()
        path = self.identity()
        self.scope.capture(path, deadline=120)
        before = len(self.calls)
        self.identity(pid=322)
        with self.assertRaises(RuntimeError):
            self.scope.capture(path, deadline=120)
        self.assertEqual(len(self.calls), before)
        self.restored()

    def test_capture_accepts_limit_exactly_and_rejects_extended_deadline(self):
        self.scope.prepare()
        path = self.identity()
        self.hook = lambda args, result: subprocess.CompletedProcess(
            result.args, 0, "x" * (2 * 1024 * 1024)
        )
        receipt = self.scope.capture(path, deadline=120)
        self.assertEqual(
            len(json.loads(receipt.read_text())["stdout"].encode()), 2 * 1024 * 1024
        )
        self.hook = None
        before = len(self.calls)
        with self.assertRaises(RuntimeError):
            self.scope.capture(path, deadline=121)
        self.assertEqual(len(self.calls), before)
        self.restored()

    def test_receipt_hardlink_rejected_and_independent_restore_remains_possible(self):
        self.scope.prepare()
        receipt = next(self.scope._output.glob("*-command.json"))
        (self.root / "receipt-link.json").hardlink_to(receipt)
        with self.assertRaises(RuntimeError):
            self.scope.capture(self.identity(), deadline=120)
        self.scope.restore(deadline=130)
        self.assertEqual(self.value, "")
        with self.assertRaises(RuntimeError):
            self.scope.require_restored()

    def test_capture_identity_hardlink_and_same_bytes_replacement_rejected(self):
        self.scope.prepare()
        identity = self.identity()
        linked = self.root / "identity-link.json"
        linked.hardlink_to(identity)
        before = len(self.calls)
        with self.assertRaises(RuntimeError):
            self.scope.capture(identity, deadline=120)
        self.assertEqual(len(self.calls), before)
        linked.unlink()
        self.scope.capture(identity, deadline=120)
        replacement = self.root / "replacement.json"
        replacement.write_bytes(identity.read_bytes())
        replacement.replace(identity)
        before = len(self.calls)
        with self.assertRaises(RuntimeError):
            self.scope.capture(identity, deadline=120)
        self.assertEqual(len(self.calls), before)
        self.restored()

    def test_post_command_receipt_drift_is_retained_after_rollback(self):
        self.scope.prepare()
        receipt = next(self.scope._output.glob("*-command.json"))

        def drift(args, result):
            if args[0] == "logcat":
                receipt.write_bytes(receipt.read_bytes() + b" ")
            return result

        self.hook = drift
        with self.assertRaises(RuntimeError):
            self.scope.capture(self.identity(), deadline=120)
        self.hook = None
        self.scope.restore(deadline=130)
        self.assertEqual(self.value, "")
        with self.assertRaises(RuntimeError):
            self.scope.require_restored()

    def test_missing_parent_and_nonfinite_deadline_never_dispatch(self):
        for deadline in (float("nan"), float("inf"), True, 100):
            with (
                self.subTest(deadline=deadline),
                self.assertRaises((ValueError, TimeoutError)),
            ):
                self.new_scope(deadline=deadline)
        with self.assertRaises(FileNotFoundError):
            self.new_scope(output=self.root / "missing/close-diagnostics")
        self.sdk.assert_not_called()

    def test_output_directory_drift_blocks_dispatch(self):
        self.scope.prepare()
        output = self.scope._output
        moved = output.with_name("moved-diagnostics")
        output.rename(moved)
        output.mkdir()
        before = len(self.calls)
        with self.assertRaises(RuntimeError):
            self.scope.capture(self.identity(), deadline=120)
        self.assertEqual(len(self.calls), before)
        output.rmdir()
        moved.rename(output)
        self.scope.restore(deadline=130)
        self.assertEqual(self.value, "")
        with self.assertRaises(RuntimeError):
            self.scope.require_restored()

    def test_mutation_receipt_publication_failure_is_not_laundered(self):
        def publish(path, record, **kwargs):
            digest = publish_new_json(path, record, **kwargs)
            if path.name == "002-command.json":
                raise OSError("completion receipt handoff failed")
            return digest

        with (
            patch(
                "tools.react_native.bleng_android_close_diagnostics.publish_new_json",
                side_effect=publish,
            ),
            self.assertRaisesRegex(OSError, "completion receipt handoff failed"),
        ):
            self.scope.prepare()
        self.assertEqual(self.value, "DEBUG")
        before = len(self.calls)
        with self.assertRaises(RuntimeError):
            self.scope.capture(self.identity(), deadline=120)
        self.assertEqual(len(self.calls), before)
        self.scope.restore(deadline=130)
        self.assertEqual(self.value, "")
        with self.assertRaises(RuntimeError):
            self.scope.require_restored()
        terminal = json.loads((self.scope._output / "restored.json").read_text())
        self.assertFalse(terminal["verified"])

    def test_restore_readback_mismatch_has_no_success_terminal(self):
        self.scope.prepare()

        def changed_after_restore(args, result):
            if args[:2] == ("shell", "setprop"):
                self.value = "WARN"
            return result

        self.hook = changed_after_restore
        with self.assertRaisesRegex(RuntimeError, "readback mismatch"):
            self.scope.restore(deadline=130)
        self.assertFalse((self.scope._output / "restored.json").exists())
        with self.assertRaises(RuntimeError):
            self.scope.require_restored()
        self.phone.require_owner()

    def test_capture_publication_cannot_outlive_its_absolute_deadline(self):
        self.scope.prepare()
        identity = self.identity()

        def publish(path, record, **kwargs):
            digest = publish_new_json(path, record, **kwargs)
            if record.get("argv", [None] * 4)[3] == "logcat":
                self.clock = 121
            return digest

        with (
            patch(
                "tools.react_native.bleng_android_close_diagnostics.publish_new_json",
                side_effect=publish,
            ),
            self.assertRaises(TimeoutError),
        ):
            self.scope.capture(identity, deadline=120)
        self.restored()


if __name__ == "__main__":
    unittest.main()
