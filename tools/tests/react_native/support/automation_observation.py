"""Shared synthetic Android 16 dumpsys fixtures for parser and session tests."""

from __future__ import annotations

PROCESSES_HEADER = "ACTIVITY MANAGER RUNNING PROCESSES (dumpsys activity processes)\n"
SERVICES_HEADER = "ACTIVITY MANAGER SERVICES (dumpsys activity services)\n"
COMPONENT = "io.appium.uiautomator2.server.test/androidx.test.runner.AndroidJUnitRunner"
TARGET = "io.appium.uiautomator2.server"


def record(
    *,
    index: int = 0,
    pid: int = 3210,
    app_id: int = 234,
    finished: bool = False,
    optional: bool = False,
) -> str:
    """Representative ActiveInstrumentation.dump with ApplicationInfo dumpFlags=0."""
    front = (
        f"        name={TARGET}.Application\n"
        "        labelRes=0x7f010001 nonLocalizedLabel=Test app icon=0x0 banner=0x0\n"
        "        permission=android.permission.INTERNET\n"
    )
    paths = (
        "        publicSourceDir=/data/app/public.apk\n"
        "        splitSourceDirs=[/data/app/split.apk]\n"
        "        splitPublicSourceDirs=[/data/app/public-split.apk]\n"
        "        resourceDirs=[]\n"
        "        overlayPaths=[]\n"
    )
    extras = (
        "      mProfileFile=/data/local/tmp/profile\n"
        "      mWatcher=android.app.IInstrumentationWatcher$Stub$Proxy@abcd\n"
        "      mUiAutomationConnection=android.app.UiAutomationConnection@abcd\n"
    )
    return (
        f"    Instrumentation #{index}: ActiveInstrumentation{{abc{index:x} {{{COMPONENT}}}"
        f"{' FINISHED' if finished else ''} 1 procs}}\n"
        f"      mClass=ComponentInfo{{{COMPONENT}}} mFinished={str(finished).lower()}\n"
        "      mRunningProcesses:\n"
        f"        #0: ProcessRecord{{deadbeef {pid}:{TARGET}/u0a{app_id}}}\n"
        f"      mTargetProcesses=[{TARGET}]\n"
        f"      mTargetInfo=ApplicationInfo{{abcd {TARGET}}}\n"
        + (front.splitlines(keepends=True)[0] if optional else "")
        + f"        packageName={TARGET}\n"
        + ("".join(front.splitlines(keepends=True)[1:]) if optional else "")
        + f"        processName={TARGET}\n"
        f"        uid={10000 + app_id} flags=0x0 privateFlags=0x0 theme=0x0\n"
        "        sourceDir=/data/app/base.apk\n"
        + (paths if optional else "")
        + f"        dataDir=/data/user/0/{TARGET}\n"
        + (
            "        classLoaderName=dalvik.system.PathClassLoader\n"
            "        splitClassLoaderNames=[null]\n"
            if optional
            else ""
        )
        + "        enabled=true minSdkVersion=23 targetSdkVersion=36 versionCode=1 targetSandboxVersion=1\n"
        "        createTimestamp=12345\n"
        + ("        knownActivityEmbeddingCerts=[]\n" + extras if optional else "")
        + "mHasBackgroundActivityStartsPermission=false\n"
        "mHasBackgroundForegroundServiceStartsPermission=true\n"
        "      mArguments=Bundle[{opaque=with spaces, punctuation={} []}]\n"
    )


def processes(body: str = "", *, tail: str = "false") -> str:
    return PROCESSES_HEADER + body + f"  mForceBackgroundCheck={tail}\n"


def history(body: str = "    app=ProcessRecord{abcd 123:example/u0a1}\n") -> str:
    return (
        SERVICES_HEADER + "  Last ANR service:\n"
        "ServiceRecord{a1b2 u10 io.appium.settings/.SettingsService}\n"
        + body
        + "\n  (nothing)\n"
    )
