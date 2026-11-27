"""Pure synthetic android-36 permission/proc/exit dumps, never device evidence."""

from __future__ import annotations

PACKAGE = "com.blehubnitrohost"
UID = 10234
PID = 2345
START_TICKS = 987654
NOW_MS = 1792238400000  # 2026-10-17 12:00:00.000 UTC
HEADER = "ACTIVITY MANAGER PROCESS EXIT INFO (dumpsys activity exit-info)\n"


def permission_dump(
    scan: bool = True, connect: bool = True, *, uid: int = UID, user: int = 0
) -> str:
    """Include a misleading install grant and another user's runtime grants."""
    return f"""Packages:
  Package [{PACKAGE}] (abc123):
    userId={uid}
    install permissions:
      android.permission.BLUETOOTH_SCAN: granted=true
      android.permission.BLUETOOTH_CONNECT: granted=true
    User {user}: ceDataInode=123 installed=true hidden=false
      runtime permissions:
        android.permission.BLUETOOTH_SCAN: granted={str(scan).lower()}, flags=[ USER_SENSITIVE_WHEN_GRANTED]
        android.permission.BLUETOOTH_CONNECT: granted={str(connect).lower()}, flags=[ USER_SENSITIVE_WHEN_DENIED]
    User 10: ceDataInode=456 installed=true hidden=false
      runtime permissions:
        android.permission.BLUETOOTH_SCAN: granted=true, flags=[]
        android.permission.BLUETOOTH_CONNECT: granted=true, flags=[]
"""


def exit_record(
    *,
    pid: int = PID,
    real_uid: int = UID,
    package_uid: int = UID,
    user: int = 0,
    process: str = PACKAGE,
    reason: int = 8,
    timestamp: str = "2026-10-17 12:00:00.100",
    index: int = 0,
) -> str:
    """ApplicationExitInfo.dump(), not toString(); sequence index is not identity."""
    name = {8: "PERMISSION CHANGE", 10: "USER REQUESTED"}.get(reason, "OTHER")
    return f"""        ApplicationExitInfo #{index}:
          timestamp={timestamp} pid={pid} realUid={real_uid} packageUid={package_uid} definingUid={package_uid} user={user}
          process={process} reason={reason} ({name}) subreason=0 (UNKNOWN) status=0
          importance=100 pss=12MB rss=34MB description=permissions revoked state=empty trace=null
"""


def exit_dump(*records: str) -> str:
    """Complete synthetic history, including the actual package/user wrappers."""
    return (
        HEADER
        + "  Last Timestamp of Persistence Into Persistent Storage: 2026-10-17 11:59:59.000\n"
        + f"  package: {PACKAGE}\n    Historical Process Exit for userId=0\n"
        + "".join(records)
    )


def proc_stat(*, pid: int = PID, ticks: int = START_TICKS) -> str:
    """Linux stat field 22 is starttime; comm may contain spaces/parentheses."""
    return f"{pid} (host (main)) S " + "0 " * 18 + f"{ticks} 0 0\n"


def process_identity() -> dict[str, int | str]:
    """Values deliberately predate the revoke clock; used by fake transports only."""
    return {
        "pid": PID,
        "uid": UID,
        "user_id": 0,
        "process_name": PACKAGE,
        "start_ticks": START_TICKS,
        "native_started_unix_ms": NOW_MS - 1000,
    }
