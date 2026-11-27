"""Install one APK through adb push and the device package manager."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import cast

from tools.android.run_instrumentation import (
    AndroidInstrumentationError,
    has_root,
    install_apk,
)
from tools.android_toolchain import adb_environment, select_test_device


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument(
        "--device", choices=("auto", "physical", "emulator"), default="auto"
    )
    _ = parser.add_argument("--serial", help="explicit adb serial")
    _ = parser.add_argument("--apk", type=Path, required=True)
    _ = parser.add_argument("--test-only", action="store_true")
    _ = parser.add_argument("--log", type=Path, required=True)
    args = parser.parse_args()
    device_mode = cast(str, args.device)
    serial = cast(str | None, args.serial)
    apk = cast(Path, args.apk)
    test_only = cast(bool, args.test_only)
    log_path = cast(Path, args.log)

    try:
        environment = adb_environment()
        if serial:
            environment["BLEHUB_ANDROID_SERIAL"] = serial
        device = select_test_device(device_mode, environment=environment)
        use_root = has_root(device.serial)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("w", encoding="utf-8") as log:
            print(f"android_apk_install_device={device.serial}", flush=True)
            print(f"android_apk_install_root={str(use_root).lower()}", flush=True)
            install_apk(
                device.serial,
                apk,
                test_only=test_only,
                use_root=use_root,
                log=log,
            )
        print("android_apk_install=passed", flush=True)
    except (AndroidInstrumentationError, OSError, ValueError) as error:
        print(f"android-apk-install: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
