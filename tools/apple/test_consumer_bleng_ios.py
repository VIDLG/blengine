"""Linked iOS consumer must contain only the intended UniFFI components."""

import json
import re
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest.mock import patch

from tools.apple import consumer_bleng_ios as consumer
from tools.apple.consumer_bleng_ios import (
    classify_result,
    validate_linked_images,
    validate_linked_symbols,
)


class BlengIOSSymbolsTests(unittest.TestCase):
    def test_linked_symbols_require_three_exact_components_without_probe(self) -> None:
        components = ("blehub", "bleng", "bleng_supervisor")
        for suffix in (
            "checksum_constructor_owner_new",
            "fn_constructor_owner_new",
            "uniffi_contract_version",
            "rustbuffer_alloc",
            "rust_future_poll_void",
        ):
            prefix = "_uniffi_" if suffix.startswith(("checksum_", "fn_")) else "_ffi_"
            lines = [f"{prefix}{component}_{suffix}\n" for component in components]
            symbols = "".join(lines)
            with self.subTest(suffix=suffix):
                validate_linked_symbols(symbols)
            for missing in lines:
                with (
                    self.subTest(suffix=suffix, missing=missing),
                    self.assertRaisesRegex(
                        RuntimeError, "unexpected iOS consumer UniFFI components"
                    ),
                ):
                    validate_linked_symbols(symbols.replace(missing, ""))
            for extra in ("toy", "bleng_supervisor_extra"):
                with (
                    self.subTest(suffix=suffix, extra=extra),
                    self.assertRaisesRegex(
                        RuntimeError, "unexpected iOS consumer UniFFI components"
                    ),
                ):
                    validate_linked_symbols(symbols + f"{prefix}{extra}_{suffix}\n")
        for extra in ("_uniffi_toy_unknown_export", "_ffi_toy_unknown_export"):
            with (
                self.subTest(unrecognized=extra),
                self.assertRaisesRegex(
                    RuntimeError, "unexpected iOS consumer UniFFI symbol"
                ),
            ):
                validate_linked_symbols(symbols + extra + "\n")
        for probe in (
            "_uniffi_bleng_foreignprobe_x",
            "_uniffi_bleng_fn_method_blengforeignprobe_run",
            "_uniffi_bleng_checksum_method_asyncprobe_run",
            "_uniffi_blehub_fn_func_ffi_test_harness_run",
            "_uniffi_bleng_async_spike_run",
        ):
            with (
                self.subTest(probe=probe),
                self.assertRaisesRegex(RuntimeError, "test-only UniFFI symbol"),
            ):
                validate_linked_symbols(symbols + probe + "\n")

    def test_staged_bindings_require_exact_three_component_surface(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bindings = root / "Sources/BleHubUniFfi"
            bindings.mkdir(parents=True)
            (root / "BleHubUniFfiNative.xcframework").mkdir()
            for relative, contents in (
                ("Package.swift", "// manifest"),
                ("BleHubUniFfiNative.xcframework/Info.plist", "// metadata"),
                ("BleHubUniFfi.flavor", "bleng\n"),
                ("Sources/BleHubUniFfi/blehub.swift", "// generated"),
                ("Sources/BleHubUniFfi/bleng.swift", "// generated"),
                ("Sources/BleHubUniFfi/bleng_supervisor.swift", "// generated"),
            ):
                (root / relative).write_text(contents, encoding="utf-8")
            with patch.object(consumer, "PACKAGE", root):
                consumer.verify_package()
                for path in sorted(bindings.iterdir()):
                    path.unlink()
                    with (
                        self.subTest(missing=path.name),
                        self.assertRaisesRegex(RuntimeError, "three-component"),
                    ):
                        consumer.verify_package()
                    path.write_text("// generated", encoding="utf-8")
                for name in ("toy.swift", "bleng_supervisor_extra.swift", "toyFFI.h"):
                    extra = bindings / name
                    extra.write_text("// unexpected", encoding="utf-8")
                    with (
                        self.subTest(extra=name),
                        self.assertRaisesRegex(RuntimeError, "binding surface"),
                    ):
                        consumer.verify_package()
                    extra.unlink()
                extra_directory = bindings / "Extra"
                extra_directory.mkdir()
                for nested_probe in (False, True):
                    if nested_probe:
                        (extra_directory / "Probe.swift").write_text(
                            "BlengForeignProbe", encoding="utf-8"
                        )
                    with (
                        self.subTest(extra_directory=True, nested_probe=nested_probe),
                        self.assertRaisesRegex(RuntimeError, "binding surface"),
                    ):
                        consumer.verify_package()
                (extra_directory / "Probe.swift").unlink()
                extra_directory.rmdir()
                external = root / "external"
                external.mkdir()
                shared = external / "Shared.swift"
                shared.write_text("// generated", encoding="utf-8")
                for name, target in (
                    ("extra.swift", shared),
                    ("Extra", external),
                    ("dangling.swift", external / "missing.swift"),
                ):
                    link = bindings / name
                    link.symlink_to(target)
                    with (
                        self.subTest(symlink=name),
                        self.assertRaisesRegex(RuntimeError, "binding surface"),
                    ):
                        consumer.verify_package()
                    link.unlink()
                for path in sorted(bindings.iterdir()):
                    path.unlink()
                    path.symlink_to(shared)
                    with (
                        self.subTest(symlink=path.name),
                        self.assertRaisesRegex(RuntimeError, "binding surface"),
                    ):
                        consumer.verify_package()
                    path.unlink()
                    path.write_text("// generated", encoding="utf-8")
                for path in sorted(bindings.iterdir()):
                    for token in (
                        "BlengForeignProbe",
                        "AsyncProbe",
                        "ffi_test_harness",
                    ):
                        path.write_text(token, encoding="utf-8")
                        with (
                            self.subTest(binding=path.name, token=token),
                            self.assertRaisesRegex(RuntimeError, "test-only"),
                        ):
                            consumer.verify_package()
                    path.write_text("// generated", encoding="utf-8")

    def test_device_result_requires_current_run_and_explicit_terminal(self) -> None:
        current = "apple_bleng_ios_run_id=current\n"
        passed = "apple_bleng_ios_artifact_consumer=passed\n"
        typed = "typed_operations=passed\n"
        self.assertIsNone(
            classify_result("apple_bleng_ios_run_id=old\n" + typed + passed, "current")
        )
        self.assertTrue(classify_result(current + typed + passed, "current"))
        for marker in (
            "",
            "typed_operations=failed\n",
            "typed_operations=passed-old\n",
        ):
            with (
                self.subTest(marker=marker),
                self.assertRaisesRegex(RuntimeError, "typed operations"),
            ):
                classify_result(current + marker + passed, "current")
        with self.assertRaisesRegex(RuntimeError, "run identity"):
            classify_result(
                "apple_bleng_ios_run_id=old\n" + current + typed + passed, "current"
            )
        with self.assertRaisesRegex(RuntimeError, "reported failure"):
            classify_result(
                current + "apple_bleng_ios_artifact_consumer=failed\n",
                "current",
            )
        with self.assertRaisesRegex(RuntimeError, "no terminal status"):
            classify_result(current, "current")

    def test_device_preflight_rejects_before_signing_when_not_connected(self) -> None:
        with (
            patch.object(consumer.consumer_ios, "executable", return_value="xcrun"),
            patch.object(
                consumer.consumer_ios,
                "device_and_team",
                side_effect=RuntimeError("expected one connected physical iPhone"),
            ),
            patch.object(
                consumer, "device_json", return_value={"result": {"devices": []}}
            ),
            patch.object(consumer, "build") as build,
            self.assertRaisesRegex(RuntimeError, "pair and unlock a wired device"),
        ):
            consumer.device_run()
        build.assert_not_called()

    def test_only_one_wired_paired_device_can_wake_tunnel(self) -> None:
        paired = {
            "identifier": "device-id",
            "hardwareProperties": {"reality": "physical", "udid": "udid"},
            "deviceProperties": {"developerModeStatus": "enabled"},
            "connectionProperties": {
                "pairingState": "paired",
                "transportType": "wired",
                "tunnelState": "disconnected",
            },
        }
        with (
            patch.dict(consumer.os.environ, {"BLEHUB_APPLE_DEVICE": "udid"}),
            patch.object(
                consumer.consumer_ios,
                "device_and_team",
                side_effect=[
                    RuntimeError("expected one connected physical iPhone"),
                    ("device-id", "udid", "TEAM"),
                ],
            ),
            patch.object(
                consumer,
                "device_json",
                side_effect=[{"result": {"devices": [paired]}}, {}],
            ) as call,
        ):
            self.assertEqual(
                consumer.connected_device("xcrun"), ("device-id", "udid", "TEAM")
            )
        self.assertEqual(
            call.call_args.args[1][:4], ["device", "info", "details", "--device"]
        )

    def test_debug_dylib_or_executable_must_be_sole_uniffi_image(self) -> None:
        symbols = (
            "_uniffi_blehub_fn_constructor_blehub_new\n"
            "_uniffi_bleng_fn_constructor_bleng_new\n"
            "_uniffi_bleng_supervisor_fn_constructor_supervisor_new\n"
        )
        validate_linked_images({"App": "_main\n", "App.debug.dylib": symbols})
        validate_linked_images({"App": symbols})
        ffi_symbols = "".join(
            f"_ffi_{component}_uniffi_contract_version\n"
            for component in ("blehub", "bleng", "bleng_supervisor")
        )
        with self.subTest(case="ffi-only owner"):
            validate_linked_images({"App": "_main\n", "App.debug.dylib": ffi_symbols})
        for images in (
            {"App": "_main\n"},
            {"App": symbols, "App.debug.dylib": symbols},
            {"App": symbols, "Second.dylib": "_ffi_blehub_uniffi_contract_version\n"},
            {"App": symbols, "Second.dylib": "_ffi_toy_rustbuffer_alloc\n"},
            {"App": symbols, "Second.dylib": "_ffi_bleng_rustbuffer_foreignprobe\n"},
            {"App": ffi_symbols, "Second.dylib": ffi_symbols},
        ):
            with (
                self.subTest(images=images),
                self.assertRaisesRegex(RuntimeError, "exactly one linked UniFFI image"),
            ):
                validate_linked_images(images)


class BlengIOSTypedWiringTests(unittest.TestCase):
    def test_native_consumers_resolve_only_their_staged_package(self) -> None:
        root = consumer.ROOT
        apple = root / "tests/consumers/apple"
        for product, package, ios_consumer in (
            ("blehub", consumer.consumer_ios.PACKAGE, consumer.consumer_ios),
            ("bleng", consumer.PACKAGE, consumer),
        ):
            with self.subTest(product=product):
                self.assertEqual(ios_consumer.SPEC, apple / product / "ios/project.yml")
            for platform, manifest in (
                ("ios", "project.yml"),
                ("macos", "Package.swift"),
            ):
                with self.subTest(product=product, platform=platform):
                    directory = apple / product / platform
                    source = (directory / manifest).read_text(encoding="utf-8")
                    paths = re.findall(r'path: ["\']?([^"\'\s\n]+)', source)
                    staged = [path for path in paths if ".work/staged/" in path]
                    expected = {package}
                    if product == "blehub":
                        expected.add(consumer.consumer_ios.BACKEND_PACKAGE)
                    self.assertEqual(len(staged), len(expected))
                    self.assertEqual(
                        {(directory / path).resolve() for path in staged}, expected
                    )
                    self.assertEqual(
                        set(staged),
                        {
                            f"../../../../../.work/staged/{path.name}"
                            for path in expected
                        },
                    )
                    self.assertNotIn("platforms/apple", source)
                    self.assertNotIn("generated/", source)
                    if platform == "macos":
                        self.assertIn('name: "BleHubUniFfi",', source)
                        self.assertIn(
                            '.product(name: "BleHubUniFfi", package: "BleHubUniFfi")',
                            source,
                        )
                    else:
                        self.assertIn("- package: BleHubUniFfi\n", source)
                        self.assertIn("product: BleHubUniFfi\n", source)

    def test_ios_compiles_shared_fixture_and_marks_only_both_successes(self) -> None:
        spec = consumer.SPEC.read_text(encoding="utf-8")
        shared = "../macos/Sources/BlengArtifactConsumer/TypedOperations.swift"
        self.assertIn(f"- path: {shared}\n        buildPhase: sources", spec)
        self.assertIn("path: ../../../../../.work/staged/apple-bleng", spec)
        self.assertTrue((consumer.SPEC.parent / shared).resolve().is_file())
        source = (consumer.SPEC.parent / "Sources/AppDelegate.swift").read_text(
            encoding="utf-8"
        )
        for declaration in (
            "enum ConsumerFailure",
            "func require(",
            "func waitForSignal(",
        ):
            self.assertIn(declaration, source)
            self.assertNotIn("private " + declaration, source)
        self.assertIn("func verifyTypedOperations() async throws", source)
        self.assertIn("try await runTypedOperationRegression()", source)
        typed = source.index("try await verifyTypedOperations()")
        smoke = source.index("try await verifyBlengArtifact()", typed)
        marker = source.index("typed_operations=passed", smoke)
        self.assertLess(typed, smoke)
        self.assertLess(marker, source.index("} catch {", smoke))


class BlengIOSDeviceSafetyTests(unittest.TestCase):
    device = "11111111-1111111111111111"
    core_device = "synthetic-core-device"
    team = "SYNTHETIC1"
    bundle = "dev.blehub.synthetic1.bleng-ios-artifact-consumer"
    executable = (
        "file:///private/var/containers/Bundle/Application/"
        "11111111-1111-4111-8111-111111111111/"
        "BlengIOSArtifactConsumer.app/BlengIOSArtifactConsumer"
    )

    def setUp(self) -> None:
        stack = self.enterContext(ExitStack())
        self.work = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.calls: list[list[str]] = []
        self.locked = False
        self.rows: Any = []
        self.receipt_device = self.core_device
        self.launch_error = False
        self.result_marker = "typed_operations=passed\n"
        self.run_id = "11111111-1111-4111-8111-111111111111"
        # Legacy default arguments must also stay inside this test's scratch area.
        stack.enter_context(
            patch.dict(consumer.device_json.__kwdefaults__, {"work": self.work})
        )
        stack.enter_context(patch.object(consumer, "WORK", self.work))
        stack.enter_context(patch.object(consumer.consumer_ios, "WORK", self.work))
        stack.enter_context(
            patch.dict(
                consumer.os.environ,
                {
                    "BLEHUB_APPLE_DEVICE": self.device,
                    "BLEHUB_APPLE_DEVELOPMENT_TEAM": self.team,
                },
            )
        )
        stack.enter_context(patch("shutil.which", side_effect=lambda name: name))
        stack.enter_context(
            patch.object(consumer.uuid, "uuid4", return_value=self.run_id)
        )
        stack.enter_context(
            patch.object(
                consumer.time, "sleep", side_effect=AssertionError("no automatic wait")
            )
        )
        stack.enter_context(
            patch.object(consumer.subprocess, "run", side_effect=self.sdk)
        )
        self.build = stack.enter_context(
            patch.object(
                consumer, "build", return_value=self.work / f"{consumer.SCHEME}.app"
            )
        )

    def sdk(
        self, command: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(command)
        if command[:2] == ["security", "find-identity"]:
            return subprocess.CompletedProcess(
                command, 0, "1 valid identities found", ""
            )
        self.assertEqual(command[:2], ["xcrun", "devicectl"])
        self.assertGreater(kwargs["timeout"], 0)
        kind = command[2:5]
        result: dict[str, Any] = {"deviceIdentifier": self.receipt_device}
        if command[2:4] == ["list", "devices"]:
            if "--json-output" not in command:
                return subprocess.CompletedProcess(
                    command, 0, f"{self.device} connected", ""
                )
            result["devices"] = [
                {
                    "identifier": self.core_device,
                    "hardwareProperties": {
                        "udid": self.device,
                        "platform": "iOS",
                        "reality": "physical",
                    },
                    "deviceProperties": {"developerModeStatus": "enabled"},
                    "connectionProperties": {"tunnelState": "connected"},
                }
            ]
        elif kind == ["device", "info", "lockState"]:
            self.assertLessEqual(kwargs["timeout"], 10)
            result.update(passcodeRequired=self.locked, unlockedSinceBoot=True)
        elif kind == ["device", "info", "processes"]:
            result["runningProcesses"] = self.rows
        elif kind == ["device", "process", "launch"]:
            self.assertNotIn("--terminate-existing", command)
        elif kind == ["device", "copy", "from"]:
            destination = Path(command[command.index("--destination") + 1])
            self.assertEqual(destination.parent, self.work)
            destination.write_text(
                f"apple_bleng_ios_run_id={self.run_id}\n"
                + self.result_marker
                + "apple_bleng_ios_artifact_consumer=passed\n",
                encoding="utf-8",
            )
        elif kind != ["device", "install", "app"]:
            self.fail(f"unexpected SDK boundary: {command}")
        document = {
            "info": {
                "outcome": "success",
                "commandType": "devicectl." + ".".join(kind),
                "arguments": command[1:],
            },
            "result": result,
        }
        output = command[command.index("--json-output") + 1]
        failed = self.launch_error and kind == ["device", "process", "launch"]
        if failed:
            document = {"error": {"description": "Locked primary launch failure"}}
        if output != "-":
            path = Path(output)
            self.assertEqual(
                path.parent, self.work, "synthetic SDK must not write parent evidence"
            )
            path.write_text(json.dumps(document), encoding="utf-8")
        return subprocess.CompletedProcess(
            command,
            17 if failed else 0,
            json.dumps(document),
            "primary launch detail" if failed else "",
        )

    def test_locked_rejects_before_signing_with_fast_readonly_budget(self) -> None:
        self.locked = True
        with self.assertRaisesRegex(RuntimeError, "locked"):
            consumer.device_run()
        self.build.assert_not_called()
        lock_queries = [
            call for call in self.calls if call[2:5] == ["device", "info", "lockState"]
        ]
        self.assertEqual(len(lock_queries), 1)
        self.assertFalse(
            any(
                call[2:4] in (["device", "install"], ["device", "process"])
                for call in self.calls
            )
        )

    def test_process_identity_refuses_existing_before_install_but_not_similar_name(
        self,
    ) -> None:
        self.rows = [{"processIdentifier": 123, "executable": self.executable}]
        with self.assertRaisesRegex(RuntimeError, "already running.*refusing takeover"):
            consumer.device_run()
        self.assertFalse(
            any(call[2:5] == ["device", "install", "app"] for call in self.calls)
        )
        self.calls.clear()
        self.rows = [
            {
                "processIdentifier": 123,
                "executable": self.executable.replace(
                    "Consumer.app/", "ConsumerOther.app/"
                ),
            }
        ]
        consumer.device_run()
        launches = [
            call for call in self.calls if call[2:5] == ["device", "process", "launch"]
        ]
        self.assertEqual(len(launches), 1)
        self.assertIn(self.bundle, launches[0])
        self.assertNotIn("--terminate-existing", launches[0])

    def test_missing_or_foreign_process_receipt_fails_closed(self) -> None:
        for rows, device in (
            (None, self.core_device),
            (["invalid"], self.core_device),
            ([], "foreign-device"),
        ):
            with self.subTest(rows=rows, device=device):
                self.rows, self.receipt_device = rows, device
                self.calls.clear()
                with self.assertRaisesRegex(RuntimeError, "process|identity"):
                    consumer.device_run()
                self.assertFalse(
                    any(
                        call[2:5] == ["device", "install", "app"] for call in self.calls
                    )
                )

    def test_launch_failure_is_single_attempt_and_preserves_primary(self) -> None:
        self.launch_error = True
        with self.assertRaisesRegex(RuntimeError, "exit 17.*primary launch detail"):
            consumer.device_run()
        self.assertEqual(
            sum(call[2:5] == ["device", "process", "launch"] for call in self.calls), 1
        )

    def test_locked_launch_sdk_failure_never_waits_or_retries(self) -> None:
        self.launch_error = True
        with self.assertRaisesRegex(RuntimeError, "exit 17.*primary launch detail"):
            consumer.device_json(
                "xcrun",
                [
                    "device",
                    "process",
                    "launch",
                    "--device",
                    self.core_device,
                    self.bundle,
                ],
                "launch.json",
                work=self.work,
            )
        self.assertEqual(len(self.calls), 1)

    def test_old_smoke_cannot_pass_device_gate(self) -> None:
        self.result_marker = ""
        with self.assertRaisesRegex(RuntimeError, "typed operations"):
            consumer.device_run()


if __name__ == "__main__":
    unittest.main()
