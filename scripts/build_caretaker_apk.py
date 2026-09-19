"""Build the caretaker debug APK with the Android SDK on this machine.

    python scripts/build_caretaker_apk.py
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ANDROID = ROOT / "android"
SDK = Path(os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
           or Path.home() / "AppData" / "Local" / "Android" / "Sdk")
JBR = Path(r"C:\Program Files\Android\Android Studio\jbr")


def main() -> int:
    java = JBR / "bin" / "java.exe"
    if not java.exists():
        print("Android Studio JBR not found; install Android Studio or set JAVA_HOME")
        return 1
    if not SDK.exists():
        print(f"Android SDK not found at {SDK}")
        return 1

    (ANDROID / "local.properties").write_text(f"sdk.dir={SDK.as_posix()}\n", encoding="utf-8")
    env = {
        **os.environ,
        "JAVA_HOME": str(JBR),
        "ANDROID_HOME": str(SDK),
        "ANDROID_SDK_ROOT": str(SDK),
    }
    cmd = [str(ANDROID / "gradlew.bat"), "assembleDebug", "--no-daemon"]
    print("building", " ".join(cmd))
    result = subprocess.run(cmd, cwd=ANDROID, env=env)
    if result.returncode != 0:
        return result.returncode
    apk = ANDROID / "app" / "build" / "outputs" / "apk" / "debug" / "app-debug.apk"
    print(f"apk  {apk}")
    return 0 if apk.exists() else 1


if __name__ == "__main__":
    raise SystemExit(main())
