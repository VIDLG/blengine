"""Protect incremental/candidate build boundaries without executing Gradle or devices."""

from __future__ import annotations

import re
import shlex
import subprocess
import unittest
from pathlib import Path

from tools import package_bleng_android as bleng_package

ROOT = Path(__file__).resolve().parents[3]


def expand(recipe: str, *arguments: str) -> list[list[str]]:
    result = subprocess.run(
        ["just", "--dry-run", "ffi", recipe, *arguments],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=True,
        timeout=15,
    )
    return [shlex.split(line) for line in result.stdout.splitlines() if line.strip()]


class AndroidBuildRecipeTests(unittest.TestCase):
    def test_blehub_consumer_build_and_install_paths_use_the_nested_lane(self) -> None:
        consumer = "tests/consumers/android/blehub"
        for recipe in ("android-backend-consumer", "android"):
            with self.subTest(recipe=recipe):
                commands = expand(recipe, "physical")
                build = next(c for c in commands if ":app:assembleDebug" in c)
                self.assertEqual(build[build.index("-p") + 1], consumer)
                self.assertIn(":app:assembleDebugAndroidTest", build)
                installs = [c for c in commands if "--app-apk" in c]
                self.assertEqual(len(installs), 1)
                command = installs[0]
                for flag, suffix in (
                    ("--app-apk", "debug/app-debug.apk"),
                    ("--test-apk", "androidTest/debug/app-debug-androidTest.apk"),
                ):
                    self.assertEqual(
                        command[command.index(flag) + 1],
                        f"{consumer}/app/build/outputs/apk/{suffix}",
                    )
                if recipe == "android":
                    self.assertTrue(
                        any(
                            "tests/ffi/runtime/android/bindings/build/outputs/apk/"
                            "androidTest/debug/bindings-debug-androidTest.apk" in c
                            for c in commands
                        )
                    )

    def test_bleng_consumer_moves_without_moving_packaging_or_output_roots(
        self,
    ) -> None:
        command = bleng_package.gradle(
            bleng_package.CONSUMER_PROJECT,
            ":app:verifyBlengArtifact",
            ":app:assembleDebug",
            ":app:assembleDebugAndroidTest",
        )
        self.assertEqual(
            Path(command[command.index("-p") + 1]),
            ROOT / "tests/consumers/android/bleng",
        )
        self.assertEqual(
            Path(command[command.index("--project-cache-dir") + 1]),
            ROOT / ".work/android-bleng/consumer/project-cache",
        )
        self.assertEqual(
            bleng_package.PACKAGING_PROJECT, ROOT / "tests/ffi/runtime/android-bleng"
        )
        self.assertEqual(
            bleng_package.MAVEN_ROOT, ROOT / ".work/staged/android-bleng-maven"
        )
        self.assertIn(":app:verifyBlengArtifact", command)

    def test_nested_consumers_resolve_only_their_staged_artifact_lane(self) -> None:
        android = ROOT / "tests/consumers/android"
        self.assertFalse((android / "settings.gradle.kts").exists())
        self.assertFalse((ROOT / "tests/consumers/android-bleng").exists())
        cases = (
            ("blehub", "settings.gradle.kts", ".work/staged/android-maven"),
            ("bleng", "settings.gradle.kts", ".work/staged/android-bleng-maven"),
            (
                "bleng",
                "build.gradle.kts",
                ".work/android-bleng/consumer/${project.name}",
            ),
            (
                "bleng",
                "app/build.gradle.kts",
                (
                    ".work/staged/android-bleng-maven/dev/blehub/bleng/0.0.0/"
                    "bleng-0.0.0.aar"
                ),
            ),
        )
        for lane, relative, expected in cases:
            with self.subTest(lane=lane, relative=relative):
                project = android / lane
                text = (project / relative).read_text(encoding="utf-8")
                paths = re.findall(r'\bfile\("(\.\./[^"\n]+)"\)', text)
                self.assertEqual(len(paths), 1)
                self.assertEqual((project / paths[0]).resolve(), ROOT / expected)
        for lane, artifact in (("blehub", "blehub-android"), ("bleng", "bleng")):
            with self.subTest(lane=lane):
                project = android / lane
                settings = (project / "settings.gradle.kts").read_text(encoding="utf-8")
                self.assertEqual(
                    re.findall(r"^include\((.*)\)$", settings, re.MULTILINE), ['":app"']
                )
                self.assertNotIn("includeBuild", settings)
                app = (project / "app/build.gradle.kts").read_text(encoding="utf-8")
                self.assertIn(f'implementation("dev.blehub:{artifact}:0.0.0")', app)
                self.assertNotRegex(app, r"\b(?:project|files)\(")
                self.assertNotIn("sourceSets", app)

    def test_default_runtime_build_is_incremental_and_stamps_only_published_artifacts(
        self,
    ) -> None:
        commands = expand("android", "physical")
        for command in commands:
            self.assertNotIn("--rerun-tasks", command)
            self.assertFalse(any(part.endswith(":clean") for part in command))
            self.assertNotEqual(command[:2], ["rm", "-rf"])
        self.assertEqual(
            sum(":library:assembleDebug" in command for command in commands), 1
        )
        self.assertEqual(
            sum(":app:assembleDebug" in command for command in commands), 1
        )

        stamps = [
            index
            for index, command in enumerate(commands)
            if command[-2:] == ["write", "android-production"]
        ]
        self.assertEqual(len(stamps), 1)
        published = next(
            index
            for index, command in enumerate(commands)
            if ":library:publishReleasePublicationToFixtureRepository" in command
        )
        consumer = next(
            index
            for index, command in enumerate(commands)
            if ":app:assembleDebug" in command
        )
        self.assertLess(published, stamps[0])
        self.assertLess(stamps[0], consumer)
        self.assertFalse(
            any(
                command[-2:] == ["write", "android-production"]
                for command in expand("android-build")
            )
        )

    def test_clean_candidate_is_explicit_scoped_and_finishes_with_consumer_build(
        self,
    ) -> None:
        commands = expand("android-candidate-build")
        self.assertEqual(len(commands), 5)
        for command, root, task in zip(
            commands[:3],
            (
                "tests/ffi/runtime/android",
                "platforms/android",
                "tests/consumers/android/blehub",
            ),
            (":bindings:clean", ":library:clean", ":app:clean"),
            strict=True,
        ):
            self.assertEqual(command[command.index("-p") + 1], root)
            self.assertIn(task, command)
        self.assertEqual(
            commands[3],
            [
                "rm",
                "-rf",
                "tests/ffi/generated/android-kotlin",
                "tests/ffi/generated/android-jni",
            ],
        )
        self.assertEqual(
            commands[4],
            ["pixi", "run", "just", "ffi", "android-backend-consumer-build"],
        )
        self.assertFalse(
            any("tools.android.run_instrumentation" in command for command in commands)
        )

    def test_native_hil_run_has_no_implicit_build_or_provision_fallback(self) -> None:
        commands = expand(
            "android-native-hil",
            "dry-run-only",
            "port with spaces",
            "contract with spaces.json",
            "profile with spaces.json",
            "120",
        )
        self.assertEqual(
            commands,
            [
                [
                    "pixi",
                    "run",
                    "-e",
                    "android",
                    "python",
                    "-m",
                    "tools.android.native_hil",
                    "--device",
                    "dry-run-only",
                    "--port",
                    "port with spaces",
                    "--contract",
                    "contract with spaces.json",
                    "--profile",
                    "profile with spaces.json",
                    "--timeout",
                    "120",
                ]
            ],
        )
