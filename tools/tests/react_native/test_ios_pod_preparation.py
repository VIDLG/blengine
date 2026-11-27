"""Pure receipt tests plus optional real-CocoaPods, synthetic-tree snapshots.

No pod install, network, build, or device operation. Set BLEHUB_COCOAPODS_RUBY
(and its GEM_HOME/GEM_PATH) to exercise the installed Ruby APIs in temp fixtures.
"""

from __future__ import annotations

import copy
import json
import os
import plistlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.react_native import ios_pod_preparation as preparation


class PreparationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.host = self.root / "host with spaces"
        self.ios = self.host / "ios"
        self.staging = self.root / "staging"
        self.ios.mkdir(parents=True)
        self.staging.mkdir()
        self.receipt = self.root / ".work/pods-preparation.json"
        self.roots = {
            "ios": str(self.ios),
            "host": str(self.host),
            "staging": str(self.staging),
        }
        self.declared = {
            "roots": self.roots,
            "packages": [{"path": "installed package", "realpath": "package root"}],
            "metadata": {"Podfile": {"sha256": "before"}},
        }
        self.installed = {
            "roots": self.roots,
            "toolchain": {"cocoapods": "1.17.0"},
            "lock": {"sha256": "same"},
            "manifest": {"sha256": "same"},
            "podfile": {"sha256": "before"},
            "specifications": {"Demo": {"selected": {"Demo": ["One.swift"]}}},
            "targets": [{"name": "Demo", "files": ["One.swift"]}],
            "integration": {"Pods.xcodeproj": {"sha256": "installed"}},
        }
        self.calls: list[list[str]] = []
        self.kwargs = {
            "ios_root": self.ios,
            "host_root": self.host,
            "staging_root": self.staging,
            "run": self.run_snapshot,
            "environment": {
                "RCT_USE_PREBUILT_RNCORE": "1",
                "ENTERPRISE_REPOSITORY": "secret-url",
            },
        }

    def run_snapshot(self, command: list[str], *, cwd: Path, timeout: int) -> str:
        self.assertEqual(cwd, self.ios)
        self.assertEqual(timeout, preparation.SNAPSHOT_TIMEOUT)
        self.assertEqual(command[:2], ["ruby", str(preparation.RUBY_HELPER)])
        self.assertEqual(
            command[3:], [str(self.ios), str(self.host), str(self.staging)]
        )
        self.calls.append(command)
        kind = command[2]
        return json.dumps(
            {
                "schema": 1,
                "kind": kind,
                "snapshot": self.declared if kind == "inputs" else self.installed,
            }
        )

    def seal(self) -> dict:
        before = preparation.begin_preparation(self.receipt, **self.kwargs)
        return preparation.seal_preparation(
            self.receipt, before=before, refresh_succeeded=True, **self.kwargs
        )

    def test_missing_receipt_never_bootstraps_or_calls_ruby(self) -> None:
        with self.assertRaisesRegex(preparation.PodPreparationError, "--refresh-pods"):
            preparation.verify_preparation(self.receipt, **self.kwargs)
        self.assertEqual(self.calls, [])
        self.assertFalse(self.receipt.exists())

    def test_seal_and_verify_are_same_snapshot_contract_without_adoption(self) -> None:
        saved = self.seal()
        self.assertEqual(saved["scope"], "source-local-installed-pods")
        raw = self.receipt.read_bytes()
        verified = preparation.verify_preparation(self.receipt, **self.kwargs)
        self.assertEqual(saved, verified)
        self.assertEqual(raw, self.receipt.read_bytes())
        self.assertEqual(
            [call[2] for call in self.calls],
            [
                "inputs",
                "inputs",
                "installed",
                "inputs",
                "inputs",
                "installed",
                "inputs",
            ],
        )
        self.assertNotIn(b"secret-url", raw)

    def test_begin_invalidates_old_receipt_even_if_inputs_then_fail(self) -> None:
        self.seal()
        with (
            patch.object(
                preparation,
                "preparation_inputs",
                side_effect=preparation.PodPreparationError("failed"),
            ),
            self.assertRaisesRegex(preparation.PodPreparationError, "failed"),
        ):
            preparation.begin_preparation(self.receipt, **self.kwargs)
        self.assertFalse(self.receipt.exists())
        with self.assertRaisesRegex(preparation.PodPreparationError, "missing"):
            preparation.verify_preparation(self.receipt, **self.kwargs)

    def test_failed_refresh_or_existing_receipt_cannot_seal(self) -> None:
        before = preparation.preparation_inputs(**self.kwargs)
        for succeeded in (False, 1, "true", None):
            with (
                self.subTest(succeeded=succeeded),
                self.assertRaisesRegex(
                    preparation.PodPreparationError, "successful explicit"
                ),
            ):
                preparation.seal_preparation(
                    self.receipt,
                    before=before,
                    refresh_succeeded=succeeded,
                    **self.kwargs,
                )
        self.seal()
        with self.assertRaisesRegex(
            preparation.PodPreparationError, "begin_preparation"
        ):
            preparation.seal_preparation(
                self.receipt, before=before, refresh_succeeded=True, **self.kwargs
            )

    def test_declared_mutation_during_install_cannot_be_sealed(self) -> None:
        before = preparation.begin_preparation(self.receipt, **self.kwargs)
        self.declared["metadata"]["Podfile"]["sha256"] = "edited"
        with self.assertRaisesRegex(
            preparation.PodPreparationError, "changed during refresh"
        ):
            preparation.seal_preparation(
                self.receipt, before=before, refresh_succeeded=True, **self.kwargs
            )
        self.assertFalse(self.receipt.exists())

    def test_derived_postinstall_outputs_can_be_new(self) -> None:
        before = preparation.begin_preparation(self.receipt, **self.kwargs)
        self.installed["integration"]["generated/new.podspec.json"] = {"sha256": "new"}
        preparation.seal_preparation(
            self.receipt, before=before, refresh_succeeded=True, **self.kwargs
        )
        preparation.verify_preparation(self.receipt, **self.kwargs)

    def test_membership_metadata_project_environment_and_toolchain_drift_rejected(
        self,
    ) -> None:
        self.seal()
        original_declared, original_installed = (
            copy.deepcopy(self.declared),
            copy.deepcopy(self.installed),
        )
        changes = [
            lambda: self.declared["metadata"].update(
                {"Added.podspec": {"sha256": "new"}}
            ),
            lambda: self.declared["packages"].append(
                {"path": "added", "realpath": "added"}
            ),
            lambda: self.installed["specifications"]["Demo"]["selected"]["Demo"].append(
                "Added.swift"
            ),
            lambda: self.installed["specifications"]["Demo"]["selected"][
                "Demo"
            ].clear(),
            lambda: self.installed["integration"]["Pods.xcodeproj"].update(
                sha256="edited"
            ),
            lambda: self.installed["toolchain"].update(cocoapods="other"),
            lambda: self.installed["targets"][0]["files"].clear(),
        ]
        for change in changes:
            self.declared, self.installed = (
                copy.deepcopy(original_declared),
                copy.deepcopy(original_installed),
            )
            change()
            with (
                self.subTest(change=change),
                self.assertRaisesRegex(preparation.PodPreparationError, "stale"),
            ):
                preparation.verify_preparation(self.receipt, **self.kwargs)
        self.declared, self.installed = original_declared, original_installed
        self.kwargs["environment"]["RCT_USE_PREBUILT_RNCORE"] = "0"
        with self.assertRaisesRegex(preparation.PodPreparationError, "environment"):
            preparation.verify_preparation(self.receipt, **self.kwargs)

    def test_transport_scope_excluded_but_supported_configuration_is_hashed(
        self,
    ) -> None:
        self.seal()
        self.kwargs["environment"].update(
            PATH="transient shim", CURL_HOME="transport", HTTPS_PROXY="secret proxy"
        )
        preparation.verify_preparation(self.receipt, **self.kwargs)
        self.kwargs["environment"]["USE_FRAMEWORKS"] = "dynamic"
        with self.assertRaisesRegex(preparation.PodPreparationError, "environment"):
            preparation.verify_preparation(self.receipt, **self.kwargs)

    def test_input_mutation_during_graph_snapshot_rejected(self) -> None:
        self.seal()
        run = self.kwargs["run"]

        def mutate(command: list[str], **kwargs: object) -> str:
            output = run(command, **kwargs)
            if command[2] == "installed":
                self.declared["metadata"]["new.rb"] = {"sha256": "changed"}
            return output

        self.kwargs["run"] = mutate
        with self.assertRaisesRegex(
            preparation.PodPreparationError, "changed during verification"
        ):
            preparation.verify_preparation(self.receipt, **self.kwargs)

    def test_bad_callback_output_and_failure_do_not_produce_receipt(self) -> None:
        values = [
            "not json",
            "[]",
            '{"schema":1,"schema":1}',
            json.dumps({"schema": 1, "kind": "installed", "snapshot": self.declared}),
            json.dumps(
                {"schema": 1, "kind": "inputs", "snapshot": {"roots": self.roots}}
            ),
        ]
        for raw in values:
            with (
                self.subTest(raw=raw),
                self.assertRaises(preparation.PodPreparationError),
            ):
                preparation.preparation_inputs(
                    **{**self.kwargs, "run": lambda *args, value=raw, **kwargs: value}
                )
        with (
            patch.object(preparation, "MAX_SNAPSHOT_BYTES", 8),
            self.assertRaisesRegex(preparation.PodPreparationError, "limit"),
        ):
            preparation.preparation_inputs(**self.kwargs)
        with self.assertRaisesRegex(preparation.PodPreparationError, "snapshot failed"):
            preparation.preparation_inputs(
                **{
                    **self.kwargs,
                    "run": lambda *args, **kwargs: (_ for _ in ()).throw(
                        TimeoutError()
                    ),
                }
            )
        self.assertFalse(self.receipt.exists())

    def test_corrupt_oversized_or_symlink_receipts_fail_before_callback(self) -> None:
        self.receipt.parent.mkdir()
        for raw in (b"broken", b"[]", b'{"schema":true}', b"\xff"):
            self.receipt.write_bytes(raw)
            with self.assertRaises(preparation.PodPreparationError):
                preparation.verify_preparation(self.receipt, **self.kwargs)
        self.receipt.write_bytes(b" " * 100)
        with (
            patch.object(preparation, "MAX_RECEIPT_BYTES", 16),
            self.assertRaisesRegex(preparation.PodPreparationError, "limit"),
        ):
            preparation.verify_preparation(self.receipt, **self.kwargs)
        self.receipt.unlink()
        target = self.root / "foreign.json"
        target.write_text("foreign")
        self.receipt.symlink_to(target)
        with self.assertRaisesRegex(preparation.PodPreparationError, "symlink"):
            preparation.begin_preparation(self.receipt, **self.kwargs)
        self.assertEqual(target.read_text(), "foreign")
        self.assertEqual(self.calls, [])

    def test_receipt_publication_failure_preserves_foreign_file_and_removes_temp(
        self,
    ) -> None:
        before = preparation.begin_preparation(self.receipt, **self.kwargs)
        with (
            patch.object(preparation.os, "link", side_effect=FileExistsError("race")),
            self.assertRaisesRegex(preparation.PodPreparationError, "publish"),
        ):
            preparation.seal_preparation(
                self.receipt, before=before, refresh_succeeded=True, **self.kwargs
            )
        self.assertEqual(list(self.receipt.parent.iterdir()), [])


FIXTURE_RUBY = r"""
require "cocoapods"
require "json"
require "digest"
require "yaml"
require "fileutils"
ios, staging = ARGV.map { |p| Pathname.new(p) }
pods = ios + "Pods"
FileUtils.mkdir_p(pods + "Local Podspecs")
FileUtils.mkdir_p(pods + "Target Support Files/Demo")
FileUtils.mkdir_p(ios + "build/generated/autolinking")
File.write(ios + "build/generated/autolinking/autolinking.json", "{}")
File.write(pods + "Target Support Files/Demo/Demo.xcconfig", "MODE = prepared\n")
json_path = pods + "Local Podspecs/Demo.podspec.json"
File.write(json_path, JSON.pretty_generate({"name"=>"Demo", "version"=>"1.0.0", "source"=>{"path"=>"."}, "source_files"=>"Sources/**/*.{swift,h}", "resources"=>"Assets/**/*", "platforms"=>{"ios"=>"16.0"}}))
lock = {"PODS"=>["Demo (1.0.0)"], "DEPENDENCIES"=>["Demo (from `#{staging}/Demo`)"], "EXTERNAL SOURCES"=>{"Demo"=>{:path=>(staging+"Demo").to_s}}, "SPEC CHECKSUMS"=>{"Demo"=>Digest::SHA1.file(json_path).hexdigest}, "PODFILE CHECKSUM"=>Digest::SHA1.file(ios+"Podfile").hexdigest, "COCOAPODS"=>Pod::VERSION}
bytes = YAML.dump(lock)
File.write(ios+"Podfile.lock", bytes)
File.write(pods+"Manifest.lock", bytes)
project = Xcodeproj::Project.new(pods+"Pods.xcodeproj")
target = project.new_target(:static_library, "Demo", :ios, "16.0")
ref = project.main_group.new_file((staging+"Demo/Sources/One.swift").to_s)
target.source_build_phase.add_file_reference(ref)
resource_ref = project.main_group.new_file((staging+"Demo/Assets/image.txt").to_s)
if ARGV[2] == "wrong_resource"
  other = project.new_target(:static_library, "Other", :ios, "16.0")
  other.resources_build_phase.add_file_reference(resource_ref)
elsif ARGV[2] != "missing_resource"
  target.resources_build_phase.add_file_reference(resource_ref)
end
mode = ARGV[2]
if mode == "subspec"
  spec = JSON.parse(File.read(json_path))
  spec["subspecs"] = [{"name"=>"Extra", "source_files"=>"Extras/**/*.swift"}]
  File.write(json_path, JSON.pretty_generate(spec))
  FileUtils.mkdir_p(staging+"Demo/Extras")
  extra = staging+"Demo/Extras/Extra.swift"
  File.write(extra, "extra source\n")
  target.source_build_phase.add_file_reference(project.main_group.new_file(extra.to_s))
  lock["PODS"] << "Demo/Extra (1.0.0)"
elsif mode == "local_podspec" || mode == "xcframework"
  FileUtils.cp_r(staging+"Demo", pods+"Demo")
  lock["EXTERNAL SOURCES"]["Demo"] = {:podspec=>(staging+"Demo/Demo.podspec").to_s}
  ref.path = (pods+"Demo/Sources/One.swift").to_s
  resource_ref.path = (pods+"Demo/Assets/image.txt").to_s
  if mode == "xcframework"
    spec = JSON.parse(File.read(json_path))
    spec["vendored_frameworks"] = "Frameworks/Demo.xcframework"
    File.write(json_path, JSON.pretty_generate(spec))
  end
elsif mode == "framework"
  spec = JSON.parse(File.read(json_path))
  spec["vendored_frameworks"] = "Frameworks/Demo.framework"
  File.write(json_path, JSON.pretty_generate(spec))
  framework = staging+"Demo/Frameworks/Demo.framework"
  FileUtils.mkdir_p(framework+"Versions/A/Resources")
  File.write(framework+"Versions/A/Resources/info.txt", "resource\n")
  File.symlink("A", framework+"Versions/Current")
  File.symlink("Versions/Current/Resources", framework+"Resources")
end
lock["SPEC CHECKSUMS"]["Demo"] = Digest::SHA1.file(json_path).hexdigest
File.write(ios+"Podfile.lock", YAML.dump(lock))
File.write(pods+"Manifest.lock", YAML.dump(lock))
project.save
"""


class RubyGraphTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        ruby = os.environ.get("BLEHUB_COCOAPODS_RUBY") or shutil.which("ruby")
        if not ruby:
            raise unittest.SkipTest(
                "no local Ruby; set BLEHUB_COCOAPODS_RUBY for CocoaPods fixture tests"
            )
        try:
            probe = subprocess.run(
                [ruby, "-e", 'require "cocoapods"'],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise unittest.SkipTest("local CocoaPods Ruby unavailable") from error
        if probe.returncode:
            raise unittest.SkipTest(
                "selected Ruby cannot require installed CocoaPods; no install attempted"
            )
        cls.ruby = ruby

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.host = self.root / "host"
        self.ios = self.host / "ios"
        self.staging = self.root / "staged"
        self.ios.mkdir(parents=True)
        (self.staging / "Demo/Sources").mkdir(parents=True)
        (self.staging / "Demo/Assets").mkdir()
        self.source = self.staging / "Demo/Sources/One.swift"
        self.source.write_text("source bytes\n")
        (self.staging / "Demo/Assets/image.txt").write_text("resource\n")
        # These must be hashed but NEVER evaluated, even during sealing.
        (self.staging / "Demo/Demo.podspec").write_text(
            'raise "CURRENT PODSPEC MUST NOT RUN"\n'
        )
        (self.ios / "Podfile").write_text('raise "PODFILE MUST NOT RUN"\n')
        (self.host / "package.json").write_text('{"name":"host"}')
        (self.host / "package-lock.json").write_text('{"packages":{}}')
        rn = self.host / "node_modules/react-native"
        (rn / "scripts").mkdir(parents=True)
        (rn / "package.json").write_text('{"name":"react-native","version":"0.87.1"}')
        (rn / "scripts/codegen.js").write_text("throw Error('MUST NOT RUN');\n")
        (self.host / "node_modules/.package-lock.json").write_text(
            '{"packages":{"node_modules/react-native":{}}}'
        )
        self.receipt = self.root / ".work/pods-preparation.json"
        self.kwargs = {
            "ios_root": self.ios,
            "host_root": self.host,
            "staging_root": self.staging,
            "run": self.run_snapshot,
            "environment": {},
        }

    def ruby_command(self, args: list[str]) -> str:
        result = subprocess.run(
            [self.ruby, *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=20,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(result.stderr.decode("utf-8", errors="replace"))
        return result.stdout.decode("utf-8")

    def run_snapshot(self, command: list[str], *, cwd: Path, timeout: int) -> str:
        self.assertEqual(cwd, self.ios)
        self.assertEqual(timeout, preparation.SNAPSHOT_TIMEOUT)
        return self.ruby_command(command[1:])

    def prepare(self, mode: str | None = None) -> None:
        before = preparation.begin_preparation(self.receipt, **self.kwargs)
        arguments = ["-e", FIXTURE_RUBY, str(self.ios), str(self.staging)]
        if mode is not None:
            arguments.append(mode)
        self.ruby_command(arguments)
        preparation.seal_preparation(
            self.receipt, before=before, refresh_succeeded=True, **self.kwargs
        )

    def test_initial_no_pods_snapshot_then_synthetic_install_and_readonly_verify(
        self,
    ) -> None:
        self.assertFalse((self.ios / "Pods").exists())
        self.prepare()
        before = self.receipt.read_bytes()
        preparation.verify_preparation(self.receipt, **self.kwargs)
        self.assertEqual(self.receipt.read_bytes(), before)

    def test_source_bytes_do_not_invalidate_but_additions_and_removals_do(self) -> None:
        self.prepare()
        self.source.write_text("different longer source bytes\n")
        preparation.verify_preparation(self.receipt, **self.kwargs)
        added = self.source.with_name("Added.swift")
        added.write_text("new\n")
        with self.assertRaisesRegex(preparation.PodPreparationError, "snapshot failed"):
            preparation.verify_preparation(self.receipt, **self.kwargs)
        added.unlink()
        self.source.unlink()
        with self.assertRaises(preparation.PodPreparationError):
            preparation.verify_preparation(self.receipt, **self.kwargs)

    def test_metadata_helper_codegen_and_support_changes_invalidate(self) -> None:
        self.prepare()
        paths = [
            self.staging / "Demo/Demo.podspec",
            self.host / "node_modules/react-native/scripts/codegen.js",
            self.ios / "Pods/Target Support Files/Demo/Demo.xcconfig",
        ]
        for path in paths:
            old = path.read_bytes()
            path.write_bytes(old + b"changed\n")
            try:
                with (
                    self.subTest(path=path),
                    self.assertRaises(preparation.PodPreparationError),
                ):
                    preparation.verify_preparation(self.receipt, **self.kwargs)
            finally:
                path.write_bytes(old)
        helper = self.staging / "Demo/new_autolinking.rb"
        helper.write_text("raise 'DO NOT RUN'\n")
        with self.assertRaisesRegex(preparation.PodPreparationError, "stale"):
            preparation.verify_preparation(self.receipt, **self.kwargs)

    def test_lock_equality_alone_never_authorizes_changed_manifest_or_json(
        self,
    ) -> None:
        self.prepare()
        manifest = self.ios / "Pods/Manifest.lock"
        manifest.write_bytes(manifest.read_bytes() + b"\n")
        with self.assertRaises(preparation.PodPreparationError):
            preparation.verify_preparation(self.receipt, **self.kwargs)
        (self.ios / "Podfile.lock").write_bytes(manifest.read_bytes())
        with self.assertRaisesRegex(preparation.PodPreparationError, "stale"):
            preparation.verify_preparation(self.receipt, **self.kwargs)
        (self.ios / "Pods/Local Podspecs/Demo.podspec.json").write_text(
            '{"name":"Demo","version":"1.0.0"}'
        )
        with self.assertRaises(preparation.PodPreparationError):
            preparation.verify_preparation(self.receipt, **self.kwargs)

    def test_selected_subspecs_are_expanded_through_real_cocoapods_consumers(
        self,
    ) -> None:
        before = preparation.begin_preparation(self.receipt, **self.kwargs)
        self.ruby_command(
            ["-e", FIXTURE_RUBY, str(self.ios), str(self.staging), "subspec"]
        )
        saved = preparation.seal_preparation(
            self.receipt, before=before, refresh_succeeded=True, **self.kwargs
        )
        selected = saved["installed"]["specifications"]["Demo"]["selected"]
        self.assertEqual(set(selected), {"Demo", "Demo/Extra"})
        self.assertTrue(
            any(
                path.endswith("Extra.swift")
                for path in selected["Demo/Extra"]["source_files"]
            )
        )
        preparation.verify_preparation(self.receipt, **self.kwargs)

    def test_local_podspec_uses_installed_source_root_without_evaluation(self) -> None:
        before = preparation.begin_preparation(self.receipt, **self.kwargs)
        self.ruby_command(
            ["-e", FIXTURE_RUBY, str(self.ios), str(self.staging), "local_podspec"]
        )
        saved = preparation.seal_preparation(
            self.receipt, before=before, refresh_succeeded=True, **self.kwargs
        )
        self.assertEqual(
            saved["installed"]["specifications"]["Demo"]["source_root"],
            str(self.ios / "Pods/Demo"),
        )
        preparation.verify_preparation(self.receipt, **self.kwargs)

    def test_framework_internal_links_and_nested_membership_are_bounded(self) -> None:
        before = preparation.begin_preparation(self.receipt, **self.kwargs)
        self.ruby_command(
            ["-e", FIXTURE_RUBY, str(self.ios), str(self.staging), "framework"]
        )
        preparation.seal_preparation(
            self.receipt, before=before, refresh_succeeded=True, **self.kwargs
        )
        preparation.verify_preparation(self.receipt, **self.kwargs)
        (
            self.staging / "Demo/Frameworks/Demo.framework/Versions/A/new.bin"
        ).write_bytes(b"new")
        with self.assertRaisesRegex(preparation.PodPreparationError, "stale"):
            preparation.verify_preparation(self.receipt, **self.kwargs)

    def test_resource_membership_is_integrated_into_the_selected_pod_target(
        self,
    ) -> None:
        self.prepare()
        for mode in ("missing_resource", "wrong_resource"):
            with self.subTest(mode=mode):
                before = preparation.begin_preparation(self.receipt, **self.kwargs)
                self.ruby_command(
                    ["-e", FIXTURE_RUBY, str(self.ios), str(self.staging), mode]
                )
                with self.assertRaisesRegex(
                    preparation.PodPreparationError, "snapshot failed"
                ):
                    preparation.seal_preparation(
                        self.receipt,
                        before=before,
                        refresh_succeeded=True,
                        **self.kwargs,
                    )
                self.assertFalse(self.receipt.exists())
                # Restore a valid fixture for the next subcase.
                self.prepare()

    def test_resource_membership_and_package_metadata_are_checked(self) -> None:
        self.prepare()
        asset = self.staging / "Demo/Assets/new.txt"
        asset.write_text("new resource")
        with self.assertRaisesRegex(
            preparation.PodPreparationError, "stale|snapshot failed"
        ):
            preparation.verify_preparation(self.receipt, **self.kwargs)
        asset.unlink()
        package = self.staging / "Demo/package.json"
        package.write_text('{"name":"new-local-metadata"}')
        with self.assertRaisesRegex(preparation.PodPreparationError, "stale"):
            preparation.verify_preparation(self.receipt, **self.kwargs)

    def test_declared_metadata_changes_reject_even_when_effective_spec_is_unchanged(
        self,
    ) -> None:
        before = preparation.begin_preparation(self.receipt, **self.kwargs)
        self.ruby_command(["-e", FIXTURE_RUBY, str(self.ios), str(self.staging)])
        (self.staging / "Demo/helper.rb").write_text("# install-time change\n")
        with self.assertRaisesRegex(
            preparation.PodPreparationError, "changed during refresh"
        ):
            preparation.seal_preparation(
                self.receipt, before=before, refresh_succeeded=True, **self.kwargs
            )
        self.assertFalse(self.receipt.exists())

    def test_bounded_ruby_expansion_rejects_oversized_tree(self) -> None:
        script = "require ARGV.shift; IosPodGraph.send(:remove_const, :MAX_VISITS); IosPodGraph.const_set(:MAX_VISITS, 1); IosPodGraph.main(ARGV)"
        with self.assertRaisesRegex(RuntimeError, "limit exceeded"):
            self.ruby_command(
                [
                    "-e",
                    script,
                    str(preparation.RUBY_HELPER),
                    "inputs",
                    str(self.ios),
                    str(self.host),
                    str(self.staging),
                ]
            )

    def add_xcframework(self) -> Path:
        framework = self.staging / "Demo/Frameworks/Demo.xcframework"
        for identifier, architecture in (
            ("ios-arm64", "arm64"),
            ("ios-arm64_x86_64-simulator", "x86_64"),
        ):
            slice_root = framework / identifier
            slice_root.mkdir(parents=True)
            (slice_root / "libdemo.a").write_bytes(b"native library bytes")
        (framework / "Info.plist").write_bytes(
            plistlib.dumps(
                {
                    "AvailableLibraries": [
                        {
                            "LibraryIdentifier": identifier,
                            "LibraryPath": "libdemo.a",
                            "SupportedArchitectures": [architecture],
                            "SupportedPlatform": "ios",
                        }
                        for identifier, architecture in (
                            ("ios-arm64", "arm64"),
                            ("ios-arm64_x86_64-simulator", "x86_64"),
                        )
                    ],
                    "XCFrameworkFormatVersion": "1.0",
                }
            )
        )
        return framework

    def test_xcframework_slice_order_is_semantic_but_metadata_is_strict(self) -> None:
        self.add_xcframework()
        self.prepare("xcframework")
        info = self.ios / "Pods/Demo/Frameworks/Demo.xcframework/Info.plist"
        original = plistlib.loads(info.read_bytes())
        # React Native's prebuilt Core pod reassembles its XCFramework during a
        # build, so slice order is not a preparation input.
        reordered = dict(original)
        reordered["AvailableLibraries"] = list(reversed(original["AvailableLibraries"]))
        info.write_bytes(plistlib.dumps(reordered))
        preparation.verify_preparation(self.receipt, **self.kwargs)
        for field, value in (
            ("SupportedPlatform", "macos"),
            ("SupportedArchitectures", ["x86_64"]),
            ("LibraryPath", "other.a"),
            ("LibraryIdentifier", "other-slice"),
        ):
            with self.subTest(field=field):
                changed = dict(original)
                changed["AvailableLibraries"] = [
                    dict(original["AvailableLibraries"][0])
                ]
                changed["AvailableLibraries"][0][field] = value
                changed["AvailableLibraries"].append(
                    dict(original["AvailableLibraries"][1])
                )
                info.write_bytes(plistlib.dumps(changed))
                with self.assertRaisesRegex(preparation.PodPreparationError, "stale"):
                    preparation.verify_preparation(self.receipt, **self.kwargs)
        info.write_bytes(plistlib.dumps(original))

    def test_xcframework_installed_metadata_not_native_bytes_is_hashed(self) -> None:
        self.add_xcframework()
        self.prepare("xcframework")
        installed = self.ios / "Pods/Demo/Frameworks/Demo.xcframework"
        info = installed / "Info.plist"
        saved = preparation.verify_preparation(self.receipt, **self.kwargs)
        members = saved["installed"]["specifications"]["Demo"]["selected"]["Demo"][
            "vendored_frameworks"
        ]
        self.assertIn("sha256", members[str(info)])
        for slice_name in ("ios-arm64", "ios-arm64_x86_64-simulator"):
            (installed / slice_name / "libdemo.a").write_bytes(
                b"rebuilt native library"
            )
        preparation.verify_preparation(self.receipt, **self.kwargs)
        original = info.read_bytes()
        for field, value in (
            ("SupportedPlatform", "macos"),
            ("SupportedArchitectures", ["x86_64"]),
            ("LibraryPath", "other.a"),
        ):
            with self.subTest(field=field):
                data = plistlib.loads(original)
                data["AvailableLibraries"][0][field] = value
                info.write_bytes(plistlib.dumps(data))
                # This is installed output, not caught by the declared scan.
                self.assertEqual(
                    saved["inputs"], preparation.preparation_inputs(**self.kwargs)
                )
                with self.assertRaisesRegex(preparation.PodPreparationError, "stale"):
                    preparation.verify_preparation(self.receipt, **self.kwargs)
                info.write_bytes(original)

    def test_xcframework_input_metadata_cannot_change_during_refresh(self) -> None:
        framework = self.add_xcframework()
        before = preparation.begin_preparation(self.receipt, **self.kwargs)
        self.ruby_command(
            ["-e", FIXTURE_RUBY, str(self.ios), str(self.staging), "xcframework"]
        )
        info = framework / "Info.plist"
        info.write_bytes(info.read_bytes() + b"\n")
        with self.assertRaisesRegex(preparation.PodPreparationError, "changed during"):
            preparation.seal_preparation(
                self.receipt, before=before, refresh_succeeded=True, **self.kwargs
            )
        self.assertFalse(self.receipt.exists())

    def test_hermes_version_properties_edits_and_removal_invalidate(self) -> None:
        version = (
            self.host
            / "node_modules/react-native/sdks/hermes-engine/version.properties"
        )
        version.parent.mkdir(parents=True)
        version.write_text("HERMES_VERSION_NAME=1\n")
        self.prepare()
        saved = preparation.verify_preparation(self.receipt, **self.kwargs)
        self.assertIn(str(version), saved["inputs"]["declared"]["metadata"])
        version.write_text("HERMES_VERSION_NAME=2\n")
        with self.assertRaisesRegex(preparation.PodPreparationError, "stale"):
            preparation.verify_preparation(self.receipt, **self.kwargs)
        version.unlink()
        with self.assertRaisesRegex(preparation.PodPreparationError, "stale"):
            preparation.verify_preparation(self.receipt, **self.kwargs)

    def add_formal_nitrogen_package(self) -> Path:
        # Mirror bleng-check's npm --prefix package run check -> generate.
        # The host has a decoy Nitrogen; the integration owns the effective one.
        root = self.root / "formal package"
        (root / "specs").mkdir(parents=True)
        (root / "package.json").write_text(
            json.dumps(
                {
                    "name": "@blengine/react-native-bleng",
                    "scripts": {
                        "check": "npm run typecheck && npm run generate",
                        "generate": "nitrogen",
                    },
                }
            )
        )
        (root / "nitro.json").write_text('{"ignorePaths":["**/node_modules"]}')
        (root / "package-lock.json").write_text('{"lockfileVersion":3}')
        (root / "specs/Demo.nitro.ts").write_text(
            'import type { Value } from "./Types"; export interface Demo { value: Value }'
        )
        (root / "specs/Types.ts").write_text("export type Value = string")
        (root / "Native.swift").write_text("native source")
        installed = {}
        for name in ("nitrogen", "type-helper"):
            package = root / "node_modules" / name
            package.mkdir(parents=True)
            (package / "package.json").write_text(
                json.dumps({"name": name, "bin": "index.js"})
            )
            (package / "index.js").write_text('throw Error("MUST NOT EXECUTE");')
            (package / "index.d.ts").write_text("export type Value = string")
            (package / "index.d.cts").write_text("export type Value = string")
            (package / "Native.cpp").write_text("native source")
            installed[f"node_modules/{name}"] = {}
        (root / "node_modules/.package-lock.json").write_text(
            json.dumps({"packages": installed})
        )
        bin_dir = root / "node_modules/.bin"
        bin_dir.mkdir()
        (bin_dir / "nitrogen").symlink_to("../nitrogen/index.js")
        logical = self.host / "node_modules/@blengine/react-native-bleng"
        logical.parent.mkdir()
        logical.symlink_to(root, target_is_directory=True)
        decoy = self.host / "node_modules/nitrogen"
        decoy.mkdir()
        (decoy / "package.json").write_text('{"name":"nitrogen"}')
        (decoy / "index.js").write_text('throw Error("HOST DECOY MUST NOT RUN");')
        lock = self.host / "node_modules/.package-lock.json"
        data = json.loads(lock.read_text())
        data["packages"]["node_modules/@blengine/react-native-bleng"] = {}
        data["packages"]["node_modules/nitrogen"] = {}
        lock.write_text(json.dumps(data))
        return root

    def test_formal_nitrogen_config_schema_and_effective_generator_drift(self) -> None:
        root = self.add_formal_nitrogen_package()
        self.prepare()
        saved = preparation.verify_preparation(self.receipt, **self.kwargs)
        metadata = saved["inputs"]["declared"]["metadata"]
        executable = root / "node_modules/.bin/nitrogen"
        self.assertEqual(
            metadata[str(executable)]["realpath"],
            str(root / "node_modules/nitrogen/index.js"),
        )
        for name in (
            "nitro.json",
            "specs/Demo.nitro.ts",
            "specs/Types.ts",
            "package-lock.json",
            "node_modules/.package-lock.json",
            "node_modules/nitrogen/index.js",
            "node_modules/type-helper/index.js",
            "node_modules/type-helper/index.d.ts",
            "node_modules/type-helper/index.d.cts",
        ):
            with self.subTest(name=name):
                path = root / name
                self.assertIn(str(path), metadata)
                original = path.read_bytes()
                path.write_bytes(original + b"\n")
                with self.assertRaisesRegex(preparation.PodPreparationError, "stale"):
                    preparation.verify_preparation(self.receipt, **self.kwargs)
                path.write_bytes(original)
        for name in ("Native.swift", "node_modules/type-helper/Native.cpp"):
            (root / name).write_text("ordinary native byte edit")
        self.source.write_text("ordinary selected native byte edit")
        preparation.verify_preparation(self.receipt, **self.kwargs)

    def test_formal_nitrogen_schema_membership_and_executable_identity(self) -> None:
        root = self.add_formal_nitrogen_package()
        self.prepare()
        added = root / "specs/Added.nitro.ts"
        added.write_text("export interface Added {}")
        with self.assertRaisesRegex(preparation.PodPreparationError, "stale"):
            preparation.verify_preparation(self.receipt, **self.kwargs)
        added.unlink()
        source = root / "specs/Demo.nitro.ts"
        original = source.read_bytes()
        source.unlink()
        with self.assertRaisesRegex(preparation.PodPreparationError, "stale"):
            preparation.verify_preparation(self.receipt, **self.kwargs)
        source.write_bytes(original)
        executable = root / "node_modules/.bin/nitrogen"
        executable.unlink()
        executable.symlink_to(self.host / "node_modules/nitrogen/index.js")
        with self.assertRaisesRegex(preparation.PodPreparationError, "snapshot failed"):
            preparation.verify_preparation(self.receipt, **self.kwargs)

    def test_formal_nitrogen_missing_local_tool_or_custom_invocation_fails_closed(
        self,
    ) -> None:
        root = self.add_formal_nitrogen_package()
        package = root / "package.json"
        original = package.read_bytes()
        data = json.loads(original)
        data["scripts"]["generate"] = "nitrogen --config custom.json"
        package.write_text(json.dumps(data))
        with self.assertRaisesRegex(preparation.PodPreparationError, "snapshot failed"):
            preparation.preparation_inputs(**self.kwargs)
        package.write_bytes(original)
        (root / "node_modules/.bin/nitrogen").unlink()
        with self.assertRaisesRegex(preparation.PodPreparationError, "snapshot failed"):
            preparation.preparation_inputs(**self.kwargs)
        self.assertFalse(self.receipt.exists())

    def test_oversized_installed_json_is_rejected_before_specification_parser(
        self,
    ) -> None:
        self.prepare()
        path = self.ios / "Pods/Local Podspecs/Demo.podspec.json"
        path.write_bytes(path.read_bytes() + b" " * 32768)
        script = r"""
require ARGV.shift
IosPodGraph.send(:remove_const, :MAX_FILE_BYTES)
IosPodGraph.const_set(:MAX_FILE_BYTES, 16384)
class << Pod::Specification
  def from_json(*)
    raise "SPECIFICATION PARSER MUST NOT RUN"
  end
end
IosPodGraph.main(ARGV)
"""
        with self.assertRaisesRegex(
            RuntimeError, "file too large: .*Demo.podspec.json"
        ):
            self.ruby_command(
                [
                    "-e",
                    script,
                    str(preparation.RUBY_HELPER),
                    "installed",
                    str(self.ios),
                    str(self.host),
                    str(self.staging),
                ]
            )

    def test_oversized_pbxproj_is_rejected_before_project_parser(self) -> None:
        self.prepare()
        path = self.ios / "Pods/Pods.xcodeproj/project.pbxproj"
        path.write_bytes(path.read_bytes() + b" " * 32768)
        script = r"""
require ARGV.shift
IosPodGraph.send(:remove_const, :MAX_FILE_BYTES)
IosPodGraph.const_set(:MAX_FILE_BYTES, 16384)
class << Xcodeproj::Project
  def open(*)
    raise "PROJECT PARSER MUST NOT RUN"
  end
end
IosPodGraph.main(ARGV)
"""
        with self.assertRaisesRegex(RuntimeError, "file too large: .*project.pbxproj"):
            self.ruby_command(
                [
                    "-e",
                    script,
                    str(preparation.RUBY_HELPER),
                    "installed",
                    str(self.ios),
                    str(self.host),
                    str(self.staging),
                ]
            )

    def test_missing_sandbox_never_created_and_interior_links_rejected(self) -> None:
        with self.assertRaises(preparation.PodPreparationError):
            preparation._snapshot(
                "installed",
                preparation._roots(self.ios, self.host, self.staging),
                self.run_snapshot,
            )
        self.assertFalse((self.ios / "Pods").exists())
        (self.staging / "foreign").symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(preparation.PodPreparationError):
            preparation.preparation_inputs(**self.kwargs)


if __name__ == "__main__":
    unittest.main()
