"""Hardware-free, fail-closed Android 16 dumpsys observation boundaries."""

from __future__ import annotations

import unittest

from tools.android.automation_observation import (
    parse_active_instrumentation,
    require_no_settings_services,
)
from tools.tests.react_native.support.automation_observation import (
    COMPONENT,
    PROCESSES_HEADER,
    SERVICES_HEADER,
    TARGET,
    history,
    processes,
    record,
)

OEM_TIME = "---- TIME 2026/11/4 17:49:26 ----\n"


class OemTimeEnvelopeTests(unittest.TestCase):
    def test_one_valid_time_prefix_preserves_empty_processes_and_services(self) -> None:
        for prefix in (
            OEM_TIME,
            "---- TIME 2024/2/29 00:00:00 ----\n",
            "---- TIME 2000/12/31 23:59:59 ----\n",
        ):
            for parser, output, expected in (
                (parse_active_instrumentation, processes(), []),
                (require_no_settings_services, SERVICES_HEADER + "  (nothing)\n", None),
                (require_no_settings_services, history(), None),
            ):
                with self.subTest(prefix=prefix, parser=parser.__name__, output=output):
                    self.assertEqual(parser(prefix + output), expected)

    def test_time_prefix_does_not_relax_envelope_or_calendar_validation(self) -> None:
        invalid_times = (
            "0000/11/4 17:49:26",
            "2026/0/4 17:49:26",
            "2026/13/4 17:49:26",
            "2026/11/0 17:49:26",
            "2026/11/31 17:49:26",
            "2026/2/29 17:49:26",
            "1900/2/29 17:49:26",
            "2026/11/4 24:00:00",
            "2026/11/4 17:60:00",
            "2026/11/4 17:49:60",
            "2026-11-4 17:49:26",
            "2026/11/4 17:49",
            "2026/11/4 17:49:26Z",
        )
        for parser, output in (
            (parse_active_instrumentation, processes()),
            (require_no_settings_services, SERVICES_HEADER + "  (nothing)\n"),
        ):
            malformed = {
                "unknown-prefix": "unknown OEM prefix\n" + output,
                "unknown-before-time": "unknown\n" + OEM_TIME + output,
                "unknown-after-time": OEM_TIME + "unknown\n" + output,
                "duplicate-time": OEM_TIME * 2 + output,
                "different-second-time": OEM_TIME
                + "---- TIME 2024/2/29 00:00:00 ----\n"
                + output,
                "missing-header": OEM_TIME + output.split("\n", 1)[1],
                "missing-footer": OEM_TIME + output.rsplit("\n", 2)[0] + "\n",
                "missing-final-lf": OEM_TIME + output.rstrip("\n"),
                "missing-time-lf": OEM_TIME.rstrip("\n") + output,
                "indented-time": " " + OEM_TIME + output,
                "trailing-time-text": OEM_TIME.replace(" ----\n", " ---- extra\n")
                + output,
                "blank-before-time": "\n" + OEM_TIME + output,
                "blank-after-time": OEM_TIME + "\n" + output,
                "crlf-time": OEM_TIME.replace("\n", "\r\n") + output,
            }
            malformed.update(
                (value, f"---- TIME {value} ----\n" + output) for value in invalid_times
            )
            for case, value in malformed.items():
                with (
                    self.subTest(parser=parser.__name__, case=case),
                    self.assertRaises(ValueError),
                ):
                    parser(value)

    def test_wrapped_active_records_keep_identity_and_never_become_empty(self) -> None:
        for finished in (False, True):
            with self.subTest(finished=finished):
                self.assertEqual(
                    parse_active_instrumentation(
                        OEM_TIME
                        + processes(
                            "  Active instrumentation:\n"
                            + record(optional=True, finished=finished)
                        )
                    ),
                    [
                        {
                            "component": COMPONENT,
                            "finished": finished,
                            "pid": 3210,
                            "process_name": TARGET,
                            "target_package": TARGET,
                            "uid": 10234,
                        }
                    ],
                )
        lines = record().splitlines(keepends=True)
        for count in range(len(lines)):
            with self.subTest(count=count), self.assertRaises(ValueError):
                parse_active_instrumentation(
                    OEM_TIME
                    + processes("  Active instrumentation:\n" + "".join(lines[:count]))
                )

    def test_wrapped_services_still_reject_live_and_transitional_sections(self) -> None:
        for section in (
            "  User 0 active services:",
            "  User 10 delayed start services:",
            "  User 0 starting in background:",
            "  Pending services:",
            "  Restarting services:",
            "  Destroying services:",
            "  Connection bindings to services:",
        ):
            for output in (
                SERVICES_HEADER + section + "\n  (nothing)\n",
                history(section + "\n    app=null\n"),
                history() + section + "\n",
            ):
                with (
                    self.subTest(section=section, output=output),
                    self.assertRaises(ValueError),
                ):
                    require_no_settings_services(OEM_TIME + output)


class ActiveInstrumentationTests(unittest.TestCase):
    def test_complete_records_preserve_all_identities_and_finished_state(self) -> None:
        self.assertEqual(
            parse_active_instrumentation(
                processes(
                    "  Active instrumentation:\n"
                    + record(optional=True)
                    + record(index=2, pid=4567, app_id=9999, finished=True)
                    + "\n  Process LRU list (sorted by oom_adj, 0 total):\n"
                )
            ),
            [
                {
                    "component": COMPONENT,
                    "finished": False,
                    "pid": 3210,
                    "process_name": TARGET,
                    "target_package": TARGET,
                    "uid": 10234,
                },
                {
                    "component": COMPONENT,
                    "finished": True,
                    "pid": 4567,
                    "process_name": TARGET,
                    "target_package": TARGET,
                    "uid": 19999,
                },
            ],
        )

    def test_target_package_is_required_and_preserved_independently_of_process(
        self,
    ) -> None:
        valid = record()
        for package in (TARGET, "example.other.target"):
            body = valid.replace(
                f"mTargetInfo=ApplicationInfo{{abcd {TARGET}}}",
                f"mTargetInfo=ApplicationInfo{{abcd {package}}}",
            ).replace(
                f"        packageName={TARGET}\n", f"        packageName={package}\n"
            )
            with self.subTest(package=package):
                parsed = parse_active_instrumentation(
                    processes("  Active instrumentation:\n" + body)
                )[0]
                self.assertEqual(parsed["target_package"], package)
                self.assertEqual(parsed["process_name"], TARGET)
        for missing in (
            f"      mTargetInfo=ApplicationInfo{{abcd {TARGET}}}\n",
            f"        packageName={TARGET}\n",
        ):
            with self.subTest(missing=missing), self.assertRaises(ValueError):
                parse_active_instrumentation(
                    processes(
                        "  Active instrumentation:\n" + valid.replace(missing, "")
                    )
                )

    def test_process_instrumentation_echo_requires_a_matching_complete_record(
        self,
    ) -> None:
        valid = record()
        summary = valid.splitlines()[0].split(": ", 1)[1]
        prefix = (
            "  All known processes:\n"
            f"  *APP* UID 10234 ProcessRecord{{abcd 3210:{TARGET}/u0a234}}\n"
            f"    mInstr={summary}\n"
            "    thread=android.app.IApplicationThread$Stub$Proxy@abcd\n\n"
        )
        self.assertEqual(
            parse_active_instrumentation(
                processes(prefix + "  Active instrumentation:\n" + valid)
            )[0]["pid"],
            3210,
        )
        for body in (
            prefix,
            prefix + "  Active instrumentation:\n" + valid.replace("abc0", "abc1"),
        ):
            with self.subTest(body=body), self.assertRaises(ValueError):
                parse_active_instrumentation(processes(body))

    def test_only_complete_envelope_without_section_is_empty(self) -> None:
        for value in ("false", "true"):
            with self.subTest(value=value):
                self.assertEqual(
                    parse_active_instrumentation(
                        processes(
                            "  All known processes:\n    ordinary process data\n",
                            tail=value,
                        )
                        + "\n"
                    ),
                    [],
                )
        for output in (
            "",
            PROCESSES_HEADER,
            processes().rstrip("\n"),
            "\n" + processes(),
            processes().replace("RUNNING PROCESSES", "PROCESSES"),
            processes(tail="unknown"),
            processes() + "extra\n",
        ):
            with self.subTest(output=output), self.assertRaises(ValueError):
                parse_active_instrumentation(output)

    def test_every_record_prefix_is_truncated_not_empty(self) -> None:
        lines = record(finished=True).splitlines(keepends=True)
        for count in range(len(lines)):
            with self.subTest(count=count), self.assertRaises(ValueError):
                parse_active_instrumentation(
                    processes("  Active instrumentation:\n" + "".join(lines[:count]))
                )

    def test_unknown_duplicate_and_reordered_fields_fail_closed(self) -> None:
        valid = record(optional=True)
        mutations = (
            valid.replace("        sourceDir=", "        mysteryDir="),
            valid.replace(
                "      mRunningProcesses:\n", "      mRunningProcesses:\n" * 2
            ),
            valid.replace("        sourceDir=/data/app/base.apk\n", ""),
            valid.replace("        createTimestamp=12345\n", "        unexpected=1\n"),
            valid.replace("mHasBackgroundActivityStartsPermission=false\n", ""),
            valid.replace(
                "mHasBackgroundForegroundServiceStartsPermission=true",
                "      mHasBackgroundForegroundServiceStartsPermission=true",
            ),
            valid.replace("      mArguments=", "      unknown="),
            valid.replace(
                "      mWatcher=", "      mProfileFile=duplicate\n      mWatcher="
            ),
            valid.replace(
                "        processName=",
                "        packageName=duplicate\n        processName=",
            ),
            valid.replace(
                "        dataDir=", "        sourceDir=duplicate\n        dataDir="
            ),
        )
        for malformed in mutations:
            with self.subTest(malformed=malformed), self.assertRaises(ValueError):
                parse_active_instrumentation(
                    processes("  Active instrumentation:\n" + malformed)
                )

    def test_count_uid_and_name_correlations_are_not_inferred(self) -> None:
        valid = record()
        mutations = (
            valid.replace("1 procs", "0 procs"),
            valid.replace("1 procs", "2 procs"),
            valid.replace("#0: ProcessRecord", "#1: ProcessRecord"),
            valid.replace("/u0a234", "/u10a234"),
            valid.replace("/u0a234", "/u0i234"),
            valid.replace("/u0a234", "/10234"),
            valid.replace("/u0a234", "/u0a10000"),
            valid.replace("3210:", "0:"),
            valid.replace("uid=10234", "uid=10235"),
            valid.replace(f"mTargetProcesses=[{TARGET}]", "mTargetProcesses=[other]"),
            valid.replace(
                f"mTargetProcesses=[{TARGET}]", f"mTargetProcesses=[{TARGET}, other]"
            ),
            valid.replace(f"processName={TARGET}", "processName=other"),
            valid.replace(
                f"ApplicationInfo{{abcd {TARGET}}}", "ApplicationInfo{abcd other}"
            ),
            valid.replace(
                "mClass=ComponentInfo{" + COMPONENT, "mClass=ComponentInfo{other/Runner"
            ),
            valid.replace("mFinished=false", "mFinished=true"),
            record(finished=True).replace(" FINISHED", ""),
            record(finished=True).replace("mFinished=true", "mFinished=false"),
            valid.replace(
                "      mTargetProcesses=",
                "        #1: ProcessRecord{abcd 99:other/u0a1}\n      mTargetProcesses=",
            ),
        )
        for malformed in mutations:
            with self.subTest(malformed=malformed), self.assertRaises(ValueError):
                parse_active_instrumentation(
                    processes("  Active instrumentation:\n" + malformed)
                )
        self.assertEqual(
            parse_active_instrumentation(
                processes("  Active instrumentation:\n" + record(app_id=0))
            )[0]["uid"],
            10000,
        )

    def test_unknown_second_record_and_orphan_records_are_never_empty(self) -> None:
        for body in (
            "  Active instrumentation:\n" + record() + record(),
            "  Active instrumentation:\n"
            + record()
            + record(index=1).replace(
                "ActiveInstrumentation{", "UnknownInstrumentation{"
            ),
            "  Active instrumentation:\n"
            + record()
            + "\n    unknown instrumentation\n",
            "  Active instrumentation:\n"
            + record()
            + "  Active instrumentation:\n"
            + record(index=1),
            "  Active instrumentation: unknown\n" + record(),
            record(),
            "  Process LRU list:\n" + record(),
            "  Active instrumentation:\n"
            + record()
            + "\n  PID mappings:\n"
            + record(index=1),
        ):
            with self.subTest(body=body), self.assertRaises(ValueError):
                parse_active_instrumentation(processes(body))

    def test_transport_control_characters_are_not_line_boundaries(self) -> None:
        for char in ("\r", "\x00", "\t", "\v", "\x1b", "\u2028"):
            with self.subTest(char=repr(char)), self.assertRaises(ValueError):
                parse_active_instrumentation(
                    processes("  Active instrumentation:\n" + record()).replace(
                        "mArguments=Bundle", f"mArguments={char}Bundle"
                    )
                )


class SettingsServicesTests(unittest.TestCase):
    def test_empty_and_unambiguously_historical_anr_are_supported(self) -> None:
        require_no_settings_services(SERVICES_HEADER + "  (nothing)\n")
        require_no_settings_services(history())
        require_no_settings_services(
            history(
                "    intent={cmp=io.appium.settings/.SettingsService}\n\n        Connections:\n"
            )
        )

    def test_missing_terminator_or_extra_nonhistorical_text_is_unsupported(
        self,
    ) -> None:
        for output in (
            "",
            SERVICES_HEADER,
            SERVICES_HEADER + "  (nothing)",
            SERVICES_HEADER + "\n  (nothing)\n",
            SERVICES_HEADER + "    app=null\n  (nothing)\n",
            SERVICES_HEADER + "  (nothing)\nextra\n",
            SERVICES_HEADER + "  (nothing)\n  (nothing)\n",
            history().replace("ServiceRecord{a1b2", "UnknownRecord{a1b2"),
            history().replace("ServiceRecord{a1b2", "    ServiceRecord{a1b2"),
            history().replace("  Last ANR service:\n", "\n  Last ANR service:\n"),
            history("  unknown historical indentation\n"),
            history(" unknown historical indentation\n"),
            history("unknown historical indentation\n"),
            history().replace("\n", "\r\n"),
        ):
            with (
                self.subTest(output=output),
                self.assertRaisesRegex(ValueError, "unsupported"),
            ):
                require_no_settings_services(output)

    def test_live_transitional_and_connection_sections_cannot_hide_in_history(
        self,
    ) -> None:
        sections = (
            "  User 0 active services:",
            "  User 10 delayed start services:",
            "  User 0 starting in background:",
            "  Pending services:",
            "  Restarting services:",
            "  Destroying services:",
            "  Connection bindings to services:",
        )
        for section in sections:
            for output in (
                SERVICES_HEADER + section + "\n  (nothing)\n",
                history(section + "\n    app=null\n"),
                history() + section + "\n",
            ):
                with self.subTest(output=output), self.assertRaises(ValueError):
                    require_no_settings_services(output)


if __name__ == "__main__":
    unittest.main()
