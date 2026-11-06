"""Pure, fail-closed parsers for the supported AOSP Android 16 dumpsys shapes.

Source baseline: platform/frameworks/base at android-16.0.0_r1:
- ProcessList.java:4495+, ActivityManagerService.java:11194+ and 11422+.
- ActiveInstrumentation.java:94-140; ApplicationInfo.java:1752-1877 (dumpFlags=0)
  and PackageItemInfo.java:443-457 define the nested body, not indentation guesses.
- ActiveServices.java:7568-7572 and 7786-8155 define Last ANR and service sections.

These parsers neither collect output nor establish session ownership. They support
one ordinary user-0 target process per instrumentation, not isolated/sandbox UIDs.
A services '(nothing)' is only a textual observation, NOT strong attestation:
AOSP service enumeration can catch an exception and only log it before printing
that marker. Collection success, package filtering, races and ownership need
independent checks by the caller; historical ANR data is not live service data.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

_PROCESSES_HEADER = "ACTIVITY MANAGER RUNNING PROCESSES (dumpsys activity processes)"
_SERVICES_HEADER = "ACTIVITY MANAGER SERVICES (dumpsys activity services)"
_PACKAGE = r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*"
_COMPONENT = _PACKAGE + r"/[A-Za-z_.$][A-Za-z0-9_.$]*"
_PROCESS = r"[A-Za-z_][A-Za-z0-9_.$]*(?::[A-Za-z0-9_.$]+)?"
_HEX = r"[0-9a-f]+"
_BOOL = r"(?:true|false)"
_SUMMARY = (
    r"    Instrumentation #(?P<index>0|[1-9][0-9]*): ActiveInstrumentation\{"
    + _HEX
    + r" \{(?P<component>"
    + _COMPONENT
    + r")\}(?P<finished> FINISHED)? 1 procs\}"
)


def _lines(output: str, header: str) -> list[str]:
    if not output.endswith("\n") or any(
        char != "\n" and not char.isprintable() for char in output
    ):
        raise ValueError("unsupported dumpsys envelope or incomplete LF output")
    lines = output[:-1].split("\n")
    timestamp = re.fullmatch(
        r"---- TIME ([0-9]{4}/[0-9]{1,2}/[0-9]{1,2} [0-9]{2}:[0-9]{2}:[0-9]{2}) ----",
        lines[0],
    )
    if timestamp is not None:
        # Observed on the physical OnePlus/API-36 controller candidate. Accept
        # exactly this one valid wrapper, never search past arbitrary preambles.
        calendar, clock = timestamp[1].split(" ")
        date(*map(int, calendar.split("/")))
        hour, minute, second = map(int, clock.split(":"))
        if hour > 23 or minute > 59 or second > 59:
            raise ValueError("invalid OEM dumpsys clock fields")
        lines = lines[1:]
    if not lines or lines[0] != header:
        raise ValueError("unsupported dumpsys envelope or incomplete LF output")
    return lines


class _Record:
    """Consume exact ordered fields; column-zero permission lines remain in-record."""

    def __init__(self, lines: list[str], position: int) -> None:
        self.lines = lines
        self.position = position

    def take(self, pattern: str) -> re.Match[str]:
        match = (
            re.fullmatch(pattern, self.lines[self.position])
            if self.position < len(self.lines)
            else None
        )
        if match is None:
            raise ValueError(
                f"unsupported or truncated instrumentation at line {self.position + 1}; "
                f"expected {pattern}"
            )
        self.position += 1
        return match

    def optional(self, pattern: str) -> None:
        if self.position < len(self.lines) and re.fullmatch(
            pattern, self.lines[self.position]
        ):
            self.position += 1

    def application_info(self, package: str, process: str, uid: int) -> None:
        """Only fields actually emitted with dumpFlags=0, in AOSP output order."""
        self.optional(r"        name=.+")
        self.take(r"        packageName=" + re.escape(package))
        self.optional(
            r"        labelRes=0x"
            + _HEX
            + r" nonLocalizedLabel=.* icon=0x"
            + _HEX
            + r" banner=0x"
            + _HEX
        )
        self.optional(r"        permission=.+")
        self.take(r"        processName=" + re.escape(process))
        self.take(
            rf"        uid={uid} flags=0x{_HEX} privateFlags=0x{_HEX} theme=0x{_HEX}"
        )
        self.take(r"        sourceDir=.+")
        self.optional(r"        publicSourceDir=.+")
        for field in (
            "splitSourceDirs",
            "splitPublicSourceDirs",
            "resourceDirs",
            "overlayPaths",
        ):
            self.optional(r"        " + field + r"=\[.*\]")
        self.take(r"        dataDir=.+")
        self.optional(r"        classLoaderName=.+")
        self.optional(r"        splitClassLoaderNames=\[.*\]")
        self.take(
            r"        enabled="
            + _BOOL
            + r" minSdkVersion=[0-9]+ targetSdkVersion=[0-9]+ versionCode=[0-9]+"
            + r" targetSandboxVersion=[0-9]+"
        )
        self.take(r"        createTimestamp=[0-9]+")
        self.optional(r"        knownActivityEmbeddingCerts=\[.*\]")


def _parse_record(lines: list[str], position: int) -> tuple[dict[str, Any], int, int]:
    record = _Record(lines, position)
    summary = record.take(_SUMMARY)
    component = summary["component"]
    finished = summary["finished"] is not None
    record.take(
        r"      mClass=ComponentInfo\{"
        + re.escape(component)
        + r"\} mFinished="
        + str(finished).lower()
    )
    record.take(r"      mRunningProcesses:")
    process = record.take(
        r"        #0: ProcessRecord\{"
        + _HEX
        + r" (?P<pid>[1-9][0-9]*):(?P<name>"
        + _PROCESS
        + r")/u0a(?P<app_id>0|[1-9][0-9]*)\}"
    )
    app_id = int(process["app_id"])
    if app_id > 9999:
        raise ValueError("unsupported instrumentation UID: not an ordinary user-0 app")
    uid = 10000 + app_id
    name = process["name"]
    record.take(r"      mTargetProcesses=\[" + re.escape(name) + r"\]")
    target = record.take(
        r"      mTargetInfo=ApplicationInfo\{"
        + _HEX
        + r" (?P<package>"
        + _PACKAGE
        + r")\}"
    )
    record.application_info(target["package"], name, uid)
    for field in ("mProfileFile", "mWatcher", "mUiAutomationConnection"):
        record.optional(r"      " + field + r"=.+")
    record.take(r"mHasBackgroundActivityStartsPermission=" + _BOOL)
    record.take(r"mHasBackgroundForegroundServiceStartsPermission=" + _BOOL)
    record.take(r"      mArguments=.*")
    return (
        {
            "component": component,
            "finished": finished,
            "pid": int(process["pid"]),
            "process_name": name,
            "target_package": target["package"],
            "uid": uid,
        },
        record.position,
        int(summary["index"]),
    )


def parse_active_instrumentation(output: str) -> list[dict]:
    """Return complete records, or raise ValueError for unsupported/malformed data.

    A valid full processes envelope without an active-instrumentation section
    returns []. Other process sections are opaque: this is not a whole-dump
    validator. All records in the active section must match the supported shape,
    including FINISHED records. Session owners, not this parser, impose a limit
    of one instrumentation session.
    """
    lines = _lines(output, _PROCESSES_HEADER)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines or not re.fullmatch(r"  mForceBackgroundCheck=" + _BOOL, lines[-1]):
        raise ValueError("unsupported or truncated processes footer")
    records: list[dict] = []
    position = 1
    seen_section = False
    in_section = False
    last_index = -1
    summaries: set[str] = set()
    process_echoes: set[str] = set()
    while position < len(lines) - 1:
        line = lines[position]
        if not in_section and line.startswith("    mInstr="):
            # ProcessRecord.dump repeats ActiveInstrumentation.toString before
            # the active section. It is an echo, never a substitute for a record.
            process_echoes.add(line.removeprefix("    mInstr="))
            position += 1
            continue
        if line == "  Active instrumentation:":
            if seen_section:
                raise ValueError("unsupported duplicate active instrumentation section")
            seen_section = in_section = True
            position += 1
            # A header-only section is not absence, even before a valid footer.
            summary_line = lines[position] if position < len(lines) else ""
            item, position, last_index = _parse_record(lines, position)
            summaries.add(summary_line.partition(": ")[2])
            records.append(item)
            continue
        if in_section:
            if line.startswith("    Instrumentation #"):
                item, position, index = _parse_record(lines, position)
                summaries.add(line.partition(": ")[2])
                if index <= last_index:
                    raise ValueError(
                        "unsupported duplicate or unordered instrumentation index"
                    )
                records.append(item)
                last_index = index
                continue
            if not line:
                position += 1
                continue
            if not line.startswith("  ") or line.startswith("   "):
                raise ValueError("unsupported instrumentation continuation")
            in_section = False
        # Never treat orphan/malformed section or record markers as absence,
        # including records concealed after an unrelated process section.
        if re.match(
            r"(?:Active instrumentation|Instrumentation\b|.*Instrumentation\{|"
            r"mClass=ComponentInfo\{|mRunningProcesses:|mTargetProcesses=|mTargetInfo=|"
            r"mHasBackground.*Permission=|mArguments=)",
            line.lstrip(" "),
            re.IGNORECASE,
        ) or line in (
            _PROCESSES_HEADER,
            "  mForceBackgroundCheck=true",
            "  mForceBackgroundCheck=false",
        ):
            raise ValueError("unsupported or orphan instrumentation data")
        position += 1
    if not process_echoes.issubset(summaries):
        raise ValueError(
            "unsupported instrumentation echo without a matching complete record"
        )
    return records


def require_no_settings_services(output: str) -> None:
    """Accept the supported empty services observation, not an absence guarantee.

    The caller must supply the settings-package-filtered output. A recognizable
    Last ANR service is historical only; any current/transitional/connection
    section or unknown history indentation raises ValueError. '(nothing)' cannot
    attest to successful enumeration (see module-level AOSP exception caveat).
    """
    lines = _lines(output, _SERVICES_HEADER)
    if lines == [_SERVICES_HEADER, "  (nothing)"]:
        return
    if (
        len(lines) < 4
        or lines[1] != "  Last ANR service:"
        or not re.fullmatch(
            r"ServiceRecord\{" + _HEX + r" u[0-9]+ " + _COMPONENT + r"(?: [^{}]*)?\}",
            lines[2],
        )
        or lines[-1] != "  (nothing)"
    ):
        raise ValueError("unsupported settings services observation; not proven empty")
    for line in lines[3:-1]:
        if line and not line.startswith("    "):
            raise ValueError(
                "unsupported settings services section or Last ANR history indentation"
            )
