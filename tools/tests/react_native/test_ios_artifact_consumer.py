"""Unit tests for the isolated React Native iOS artifact consumer."""

from __future__ import annotations

import io
import json
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.react_native import ios_artifact_consumer as consumer


class IosArtifactConsumerTests(unittest.TestCase):
    def _fixture_roots(self, root: Path) -> tuple[Path, Path, Path]:
        integration = root / "integrations/react-native-internal"
        apple_pods = integration / "apple-pods"
        host = root / "tests/consumers/react-native-internal"

        for relative in consumer.INTEGRATION_FILES:
            path = integration / relative
            if path.suffix:
                path.parent.mkdir(parents=True, exist_ok=True)
                if path.name == "package.json":
                    path.write_text(
                        json.dumps(
                            {
                                "name": consumer.PACKAGE_NAME,
                                "version": "0.0.0",
                                "dependencies": {},
                            }
                        ),
                        encoding="utf-8",
                    )
                else:
                    path.write_text("fixture\n", encoding="utf-8")
            else:
                path.mkdir(parents=True, exist_ok=True)
        (
            integration / "nitrogen/generated/ios/BleHubReactNative+autolinking.rb"
        ).write_text("def add_nitrogen_files(spec); end\n", encoding="utf-8")
        generated_shared = integration / "nitrogen/generated/shared/c++"
        generated_shared.mkdir(parents=True, exist_ok=True)
        (generated_shared / "HybridBleHubProbeSpec.hpp").write_text(
            "// generated\n", encoding="utf-8"
        )
        (integration / "android").mkdir(parents=True)
        (integration / "android/secret.cpp").write_text(
            "must not stage", encoding="utf-8"
        )

        for relative in consumer.APPLE_POD_FILES:
            path = apple_pods / relative
            if relative.endswith((".xcframework", "Sources")):
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("pod fixture\n", encoding="utf-8")
        apple_source = apple_pods / "BleHubApple/Sources/BleHubApple"
        apple_source.mkdir(parents=True, exist_ok=True)
        (apple_source / "Backend.swift").write_text(
            "// Apple source\n", encoding="utf-8"
        )
        uniffi_source = apple_pods / "BleHubUniFfi/Sources/BleHubUniFfi"
        uniffi_source.mkdir(parents=True, exist_ok=True)
        (uniffi_source / "blehub.swift").write_text(
            "// UniFFI source\n", encoding="utf-8"
        )
        (
            apple_pods / "BleHubUniFfi/BleHubUniFfiNative.xcframework/Info.plist"
        ).write_text("fixture\n", encoding="utf-8")
        (apple_pods / "BleHubUniFfi/BleHubUniFfiNative.xcframework/ios-arm64").mkdir()
        (
            apple_pods
            / "BleHubUniFfi/BleHubUniFfiNative.xcframework/ios-arm64/libblehub_uniffi.a"
        ).write_bytes(
            b"native image with build metadata " + str(consumer.PROJECT_ROOT).encode()
        )
        (apple_pods / "BleHubUniFfi/.build/index-build/debug.yaml").parent.mkdir(
            parents=True, exist_ok=True
        )
        (apple_pods / "BleHubUniFfi/.build/index-build/debug.yaml").write_text(
            "must not stage\n", encoding="utf-8"
        )

        for relative in consumer.HOST_FILES:
            path = host / relative
            if relative == "package.json":
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps(
                        {
                            "name": "host",
                            "dependencies": {
                                consumer.PACKAGE_NAME: "file:../../../integrations/react-native-internal"
                            },
                        }
                    ),
                    encoding="utf-8",
                )
            elif relative == "package-lock.json":
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps(
                        {
                            "name": "host",
                            "lockfileVersion": 3,
                            "packages": {
                                "": {
                                    "dependencies": {
                                        consumer.PACKAGE_NAME: "file:../../../integrations/react-native-internal"
                                    }
                                },
                                "../../../integrations/react-native-internal": {
                                    "name": consumer.PACKAGE_NAME
                                },
                                f"node_modules/{consumer.PACKAGE_NODE_NAME}": {
                                    "resolved": "../../../integrations/react-native-internal",
                                    "link": True,
                                },
                            },
                        }
                    ),
                    encoding="utf-8",
                )
            elif relative == "ios/.xcode.env":
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    "export NODE_BINARY=$(command -v node)\n", encoding="utf-8"
                )
            elif relative == "src":
                path.mkdir(parents=True, exist_ok=True)
                (path / "App.tsx").write_text("export {}\n", encoding="utf-8")
            elif relative.endswith(
                (".xcodeproj", "BleHubNitroHost", "BleHubNitroHostUITests")
            ):
                path.mkdir(parents=True, exist_ok=True)
                (path / "fixture").write_text("fixture\n", encoding="utf-8")
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("fixture\n", encoding="utf-8")
        (host / "tsconfig.json").write_text(
            json.dumps(
                {
                    "compilerOptions": {
                        "paths": {
                            consumer.PACKAGE_NAME: [
                                "../../../integrations/react-native-internal/src"
                            ]
                        },
                        "baseUrl": ".",
                        "typeRoots": [
                            "../../../integrations/react-native-internal/node_modules/@types"
                        ],
                        "strict": True,
                    },
                    "include": ["src/**/*.ts", "src/**/*.tsx"],
                }
            ),
            encoding="utf-8",
        )
        return integration, apple_pods, host

    def test_stage_package_is_explicit_and_excludes_android_and_build_output(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            integration, apple_pods, _ = self._fixture_roots(root)
            layout = consumer.make_layout(root / "work")
            layout.package_root.mkdir(parents=True)

            consumer.stage_package(
                layout, integration_root=integration, apple_pods_root=apple_pods
            )

            self.assertTrue(
                (
                    layout.package_root
                    / "nitrogen/generated/ios/BleHubReactNative+autolinking.rb"
                ).is_file()
            )
            self.assertTrue(
                (
                    layout.package_root
                    / "apple-pods/BleHubUniFfi/BleHubUniFfiNative.xcframework"
                ).is_dir()
            )
            self.assertFalse((layout.package_root / "android").exists())
            self.assertFalse(
                (layout.package_root / "apple-pods/BleHubUniFfi/.build").exists()
            )
            native_root = "BleHubUniFfi/BleHubUniFfiNative.xcframework"
            source = apple_pods / native_root
            staged = layout.package_root / "apple-pods" / native_root
            self.assertEqual(
                consumer._sha256_tree(source), consumer._sha256_tree(staged)
            )
            self.assertIn(
                str(consumer.PROJECT_ROOT).encode(),
                (staged / "ios-arm64/libblehub_uniffi.a").read_bytes(),
            )

    def test_create_host_rewrites_only_disposable_copy_and_removes_source_paths(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            integration, apple_pods, host_source = self._fixture_roots(root)
            _ = integration, apple_pods
            layout = consumer.make_layout(root / "work")
            layout.host_root.mkdir(parents=True)
            tarball = layout.artifact_root / "blehub-react-native-internal-0.0.0.tgz"
            tarball.parent.mkdir(parents=True)
            tarball.write_bytes(b"tarball")

            consumer.create_host(layout, tarball, source_root=host_source)

            package = json.loads((layout.host_root / "package.json").read_text())
            self.assertEqual(
                package["dependencies"][consumer.PACKAGE_NAME],
                "file:../artifacts/blehub-react-native-internal-0.0.0.tgz",
            )
            lock = json.loads((layout.host_root / "package-lock.json").read_text())
            self.assertNotIn("integrations/react-native-internal", json.dumps(lock))
            self.assertNotIn(
                "paths",
                json.loads((layout.host_root / "tsconfig.json").read_text())[
                    "compilerOptions"
                ],
            )
            self.assertNotIn(
                "typeRoots",
                json.loads((layout.host_root / "tsconfig.json").read_text())[
                    "compilerOptions"
                ],
            )
            self.assertNotIn(
                "integrations/react-native-internal",
                (layout.host_root / "metro.config.js").read_text(),
            )
            rn_config = (layout.host_root / "react-native.config.js").read_text()
            self.assertIn(
                "require.resolve('@blehub/react-native-internal/package.json')",
                rn_config,
            )
            self.assertNotIn("integrations/react-native-internal", rn_config)
            podfile = (layout.host_root / "ios/Podfile").read_text()
            self.assertIn(
                'require.resolve("@blehub/react-native-internal/package.json"', podfile
            )
            self.assertIn(
                "File.join(integration_root, 'apple-pods', 'BleHubUniFfi')", podfile
            )
            self.assertNotIn(str(root), podfile)
            self.assertFalse((layout.host_root / "ios/Podfile.lock").exists())

    def test_actual_native_host_stages_without_checkout_header_dependencies(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = consumer.make_layout(Path(directory) / "work")
            layout.host_root.mkdir(parents=True)
            tarball = layout.artifact_root / "blehub-react-native-internal-0.0.0.tgz"
            native_source = Path("ios/BleHubNitroHost/BleHubPendingReloadLifecycle.mm")
            source_path = consumer.HOST_SOURCE_ROOT / native_source
            original = source_path.read_bytes()

            consumer.create_host(layout, tarball)

            staged = (layout.host_root / native_source).read_bytes()
            self.assertEqual(staged, original)
            self.assertEqual(source_path.read_bytes(), original)
            self.assertIn(
                b"#include <BleHubReactNative/HybridBleHubProbeSpecSwift.hpp>", staged
            )
            consumer._assert_no_checkout_leakage(layout.host_root)

    def _linker_fixture(self, root: Path) -> tuple[consumer.Layout, Path, Path]:
        layout = consumer.make_layout(root / "work")
        source = root / "source/BleHubUniFfiNative.xcframework"
        installed = (
            layout.host_root
            / "node_modules/@blehub/react-native-internal/apple-pods/"
            / "BleHubUniFfi/BleHubUniFfiNative.xcframework"
        )
        source_slices = {
            "ios-arm64_x86_64-simulator/libblehub_uniffi.a": b"simulator archive",
            "ios-arm64/libblehub_uniffi.a": b"device archive",
        }
        for relative, payload in source_slices.items():
            source_file = source / relative
            installed_file = installed / relative
            source_file.parent.mkdir(parents=True, exist_ok=True)
            installed_file.parent.mkdir(parents=True, exist_ok=True)
            source_file.write_bytes(payload)
            installed_file.write_bytes(payload)

        config = (
            layout.host_root
            / "ios/Pods/Target Support Files/Pods-BleHubNitroHost/"
            / "Pods-BleHubNitroHost.debug.xcconfig"
        )
        config.parent.mkdir(parents=True, exist_ok=True)
        simulator_archive = installed / "ios-arm64_x86_64-simulator/libblehub_uniffi.a"
        device_archive = installed / "ios-arm64/libblehub_uniffi.a"
        config.write_text(
            "\n".join(
                [
                    f'OTHER_LDFLAGS[sdk=iphonesimulator*] = $(inherited) -force_load "{simulator_archive}"',
                    f'OTHER_LDFLAGS[sdk=iphoneos*] = $(inherited) -force_load "{device_archive}"',
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        return layout, source, config

    def test_rn_cli_must_resolve_the_installed_tarball_module(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = consumer.make_layout(Path(directory) / "work")
            installed = layout.host_root / "node_modules/@blehub/react-native-internal"
            installed.mkdir(parents=True)
            expected = {
                "dependencies": {
                    consumer.PACKAGE_NAME: {
                        "root": str(installed),
                        "platforms": {
                            "ios": {
                                "podspecPath": str(
                                    installed / "BleHubReactNative.podspec"
                                )
                            }
                        },
                    }
                }
            }
            with patch.object(consumer, "_run_quiet") as command:
                command.return_value.stdout = json.dumps(expected)
                consumer.install_dependencies(layout)
                self.assertEqual(command.call_count, 3)
                expected["dependencies"][consumer.PACKAGE_NAME]["root"] = str(
                    consumer.INTEGRATION_ROOT
                )
                command.return_value.stdout = json.dumps(expected)
                with self.assertRaisesRegex(
                    consumer.ArtifactConsumerError, "outside the installed artifact"
                ):
                    consumer.install_dependencies(layout)

    def test_pod_linker_provenance_accepts_installed_archives(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout, source, _ = self._linker_fixture(Path(directory))
            consumer._assert_pod_linker_provenance(layout, source_xcframework=source)

    def test_pod_linker_provenance_rejects_wrong_path_and_duplicate_flag(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout, source, config = self._linker_fixture(Path(directory))
            content = config.read_text(encoding="utf-8")
            wrong_path = "/wrong/package/libblehub_uniffi.a"
            expected_path = (
                layout.host_root
                / "node_modules/@blehub/react-native-internal/apple-pods/"
                / "BleHubUniFfi/BleHubUniFfiNative.xcframework/ios-arm64_x86_64-simulator/libblehub_uniffi.a"
            )
            config.write_text(
                content.replace(str(expected_path), wrong_path), encoding="utf-8"
            )
            with self.assertRaisesRegex(
                consumer.ArtifactConsumerError, "exactly one expected"
            ):
                consumer._assert_pod_linker_provenance(
                    layout, source_xcframework=source
                )

            layout, source, config = self._linker_fixture(Path(directory) / "duplicate")
            content = config.read_text(encoding="utf-8")
            first_line, second_line = content.splitlines()
            duplicate = first_line + " " + first_line[first_line.index("-force_load") :]
            config.write_text(f"{duplicate}\n{second_line}\n", encoding="utf-8")
            with self.assertRaises(consumer.ArtifactConsumerError):
                consumer._assert_pod_linker_provenance(
                    layout, source_xcframework=source
                )

    def test_pod_linker_provenance_rejects_old_paths_missing_and_changed_archives(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for label in ("checkout", "staged"):
                layout, source, config = self._linker_fixture(root / label)
                wrong_root = (
                    consumer.INTEGRATION_ROOT
                    if label == "checkout"
                    else layout.package_root
                )
                expected_path = (
                    layout.host_root
                    / "node_modules/@blehub/react-native-internal/apple-pods/"
                    / "BleHubUniFfi/BleHubUniFfiNative.xcframework/ios-arm64/libblehub_uniffi.a"
                )
                replacement = (
                    wrong_root
                    / "apple-pods/BleHubUniFfi/BleHubUniFfiNative.xcframework/ios-arm64/libblehub_uniffi.a"
                )
                config.write_text(
                    config.read_text(encoding="utf-8").replace(
                        str(expected_path), str(replacement)
                    ),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    consumer.ArtifactConsumerError, "forbidden checkout/staged paths"
                ):
                    consumer._assert_pod_linker_provenance(
                        layout, source_xcframework=source
                    )

            layout, source, _ = self._linker_fixture(root / "missing")
            installed_device = (
                layout.host_root
                / "node_modules/@blehub/react-native-internal/apple-pods/"
                / "BleHubUniFfi/BleHubUniFfiNative.xcframework/ios-arm64/libblehub_uniffi.a"
            )
            installed_device.unlink()
            with self.assertRaises(consumer.ArtifactConsumerError):
                consumer._assert_pod_linker_provenance(
                    layout, source_xcframework=source
                )

            layout, source, _ = self._linker_fixture(root / "changed")
            installed_device = (
                layout.host_root
                / "node_modules/@blehub/react-native-internal/apple-pods/"
                / "BleHubUniFfi/BleHubUniFfiNative.xcframework/ios-arm64/libblehub_uniffi.a"
            )
            installed_device.write_bytes(b"tampered")
            with self.assertRaises(consumer.ArtifactConsumerError):
                consumer._assert_pod_linker_provenance(
                    layout, source_xcframework=source
                )

    def _pack_fixture(
        self,
        staged: Path,
        tarball: Path,
        *,
        replace: dict[str, bytes] | None = None,
        omit: set[str] | None = None,
    ) -> None:
        with tarfile.open(tarball, "w:gz") as archive:
            for file in sorted(staged.rglob("*")):
                if not file.is_file():
                    continue
                name = "package/" + file.relative_to(staged).as_posix()
                if name in (omit or set()):
                    continue
                payload = (replace or {}).get(name, file.read_bytes())
                info = tarfile.TarInfo(name)
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))

    def test_archive_hashes_prove_unmodified_native_files_and_reject_tampering(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            integration, pods, _ = self._fixture_roots(root)
            layout = consumer.make_layout(root / "work")
            layout.package_root.mkdir(parents=True)
            consumer.stage_package(
                layout, integration_root=integration, apple_pods_root=pods
            )
            source = pods / "BleHubUniFfi/BleHubUniFfiNative.xcframework"
            staged = (
                layout.package_root
                / "apple-pods/BleHubUniFfi/BleHubUniFfiNative.xcframework"
            )
            archive_path = "package/apple-pods/BleHubUniFfi/BleHubUniFfiNative.xcframework/ios-arm64/libblehub_uniffi.a"
            tarball = root / "package.tgz"
            self._pack_fixture(layout.package_root, tarball)
            consumer.verify_tarball(
                tarball, source_xcframework=source, staged_xcframework=staged
            )

            self._pack_fixture(
                layout.package_root, tarball, replace={archive_path: b"changed"}
            )
            with self.assertRaisesRegex(
                consumer.ArtifactConsumerError, "changed=.*libblehub_uniffi.a"
            ):
                consumer.verify_tarball(
                    tarball, source_xcframework=source, staged_xcframework=staged
                )

            self._pack_fixture(layout.package_root, tarball, omit={archive_path})
            with self.assertRaisesRegex(
                consumer.ArtifactConsumerError, "missing=.*libblehub_uniffi.a"
            ):
                consumer.verify_tarball(
                    tarball, source_xcframework=source, staged_xcframework=staged
                )

            (staged / "ios-arm64/libblehub_uniffi.a").write_bytes(b"changed")
            with self.assertRaisesRegex(
                consumer.ArtifactConsumerError, "source versus staged"
            ):
                consumer.verify_tarball(
                    tarball, source_xcframework=source, staged_xcframework=staged
                )

    def test_textual_checkout_leaks_are_rejected_not_binary_build_metadata(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            integration, pods, _ = self._fixture_roots(root)
            layout = consumer.make_layout(root / "work")
            layout.package_root.mkdir(parents=True)
            consumer.stage_package(
                layout, integration_root=integration, apple_pods_root=pods
            )
            source = pods / "BleHubUniFfi/BleHubUniFfiNative.xcframework"
            staged = (
                layout.package_root
                / "apple-pods/BleHubUniFfi/BleHubUniFfiNative.xcframework"
            )
            tarball = root / "package.tgz"
            leaked = "package/BleHubReactNative.podspec"
            self._pack_fixture(
                layout.package_root,
                tarball,
                replace={leaked: str(consumer.PROJECT_ROOT).encode()},
            )
            with self.assertRaisesRegex(
                consumer.ArtifactConsumerError, "checkout path leakage"
            ):
                consumer.verify_tarball(
                    tarball, source_xcframework=source, staged_xcframework=staged
                )
            (staged / "Info.plist").write_text(
                str(consumer.PROJECT_ROOT), encoding="utf-8"
            )
            with self.assertRaisesRegex(
                consumer.ArtifactConsumerError, "source versus staged"
            ):
                consumer.verify_tarball(
                    tarball, source_xcframework=source, staged_xcframework=staged
                )

    def test_existing_run_evidence_is_preserved_and_reuse_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(consumer, "PROJECT_ROOT", root):
                parent = root / ".work/react-native-ios-artifact-consumer"
                parent.mkdir(parents=True)
                first = consumer._new_run_root(parent)
                layout = consumer.make_layout(first)
                consumer._prepare_work_tree(layout)
                evidence = layout.log_root / "previous.log"
                evidence.write_text("keep", encoding="utf-8")
                with self.assertRaisesRegex(
                    consumer.ArtifactConsumerError, "refusing to reuse"
                ):
                    consumer._prepare_work_tree(layout)
                second = consumer._new_run_root(parent)
                self.assertNotEqual(first, second)
                consumer._prepare_work_tree(consumer.make_layout(second))
                self.assertEqual(evidence.read_text(encoding="utf-8"), "keep")
                with self.assertRaises(consumer.ArtifactConsumerError):
                    consumer._prepare_work_tree(consumer.make_layout(root / ".work"))

    def test_command_runner_never_inherits_interactive_stdin(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(
                consumer.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(["xcodebuild"], 0, "", ""),
            ) as run:
                consumer._run_quiet(
                    ["xcodebuild"],
                    cwd=root,
                    log=root / "command.log",
                    timeout=1,
                )
            self.assertIs(run.call_args.kwargs["stdin"], subprocess.DEVNULL)
            self.assertTrue(run.call_args.kwargs["start_new_session"])

    def test_simulator_build_is_unsigned_and_never_a_device_or_archive_build(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = consumer.make_layout(Path(directory) / "work")
            with patch.object(
                consumer, "_require_executable", return_value="xcodebuild"
            ):
                command = consumer.simulator_build_command(layout)
            self.assertIn("generic/platform=iOS Simulator", command)
            self.assertIn("CODE_SIGNING_ALLOWED=NO", command)
            self.assertIn("CODE_SIGNING_REQUIRED=NO", command)
            self.assertNotIn("archive", command)
            self.assertNotIn("iphoneos", command)
            self.assertNotIn("devicectl", command)
            device_command = consumer.device_build_command(layout)
            self.assertIn("generic/platform=iOS", device_command)
            self.assertIn("iphoneos", device_command)
            self.assertIn(str(layout.device_derived_data), device_command)
            self.assertNotIn(str(layout.simulator_derived_data), device_command)
            self.assertIn("CODE_SIGNING_ALLOWED=NO", device_command)
            self.assertNotIn("archive", device_command)
            signed = consumer.signed_device_build_command(
                layout, "00008110-000C61160C39401E", "FWVYQ287W5"
            )
            self.assertIn("id=00008110-000C61160C39401E", signed)
            self.assertIn("DEVELOPMENT_TEAM=FWVYQ287W5", signed)
            self.assertIn("CODE_SIGN_STYLE=Automatic", signed)
            self.assertNotIn("CODE_SIGNING_ALLOWED=NO", signed)
            self.assertNotIn("CODE_SIGNING_REQUIRED=NO", signed)
            with self.assertRaises(consumer.ArtifactConsumerError):
                consumer.signed_device_build_command(layout, "simulator", "FWVYQ287W5")
            with self.assertRaises(consumer.ArtifactConsumerError):
                consumer.signed_device_build_command(
                    layout, "00008110-000C61160C39401E", "bad-team"
                )

    def test_ui_gate_does_not_precede_xctest_with_duplicate_runtime_launch(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = consumer.make_layout(Path(directory) / "work")
            signed_app = (
                layout.device_derived_data
                / "Build/Products/Debug-iphoneos"
                / consumer.APP_NAME
            )
            signed_app.mkdir(parents=True)
            (signed_app / "embedded.mobileprovision").write_bytes(b"profile")
            (signed_app / f"{consumer.SCHEME}.debug.dylib").write_bytes(b"binary")
            with (
                patch.object(
                    consumer, "_require_executable", return_value="xcodebuild"
                ),
                patch.object(consumer, "_run_quiet") as run,
                patch.object(
                    consumer, "ui_test_command", return_value=["xcodebuild", "test"]
                ),
            ):
                consumer.build_and_check(
                    layout,
                    device=("00008110-000C61160C39401E", "FWVYQ287W5"),
                    ui_test=True,
                )
            commands = [call.args[0] for call in run.call_args_list]
            self.assertEqual(commands[-1], ["xcodebuild", "test"])
            self.assertFalse(
                any("run_ios_device_runtime.py" in str(command) for command in commands)
            )

    def test_ui_test_is_a_bounded_physical_xctest_and_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            layout = consumer.make_layout(Path(directory) / "work")
            with patch.object(
                consumer, "_require_executable", return_value="xcodebuild"
            ):
                command = consumer.ui_test_command(
                    layout, "00008110-000C61160C39401E", "FWVYQ287W5"
                )
            self.assertIn("-workspace", command)
            self.assertIn(
                str(layout.host_root / "ios/BleHubNitroHost.xcworkspace"), command
            )
            self.assertIn("-destination", command)
            self.assertIn("id=00008110-000C61160C39401E", command)
            self.assertIn("DEVELOPMENT_TEAM=FWVYQ287W5", command)
            self.assertIn(
                "-only-testing:BleHubNitroHostUITests/BleHubNitroHostUITests/testContractLabCompletesAndRecreatesProbe",
                command,
            )
            self.assertEqual(command[-1], "test")
            self.assertNotIn("archive", command)
            self.assertNotIn("devicectl", command)
            with self.assertRaises(consumer.ArtifactConsumerError):
                consumer.ui_test_command(layout, "simulator", "FWVYQ287W5")
            with self.assertRaises(consumer.ArtifactConsumerError):
                consumer.ui_test_command(
                    layout, "00008110-000C61160C39401E", "bad-team"
                )


if __name__ == "__main__":
    unittest.main()
