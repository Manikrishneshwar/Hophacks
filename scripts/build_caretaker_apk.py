"""Build the caretaker debug APK.

    python scripts/build_caretaker_apk.py

On Windows this uses Android Studio's JBR and the user SDK. On Linux it
will download a portable JDK 17 and the Android command-line tools into
the home directory if they are not already there.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ANDROID = ROOT / "android"

WIN_SDK = Path.home() / "AppData" / "Local" / "Android" / "Sdk"
WIN_JBR = Path(r"C:\Program Files\Android\Android Studio\jbr")
LINUX_SDK = Path.home() / "Android" / "Sdk"
LINUX_JDK = Path.home() / ".local" / "jdk-17"

JDK_URL = (
    "https://github.com/adoptium/temurin17-binaries/releases/download/"
    "jdk-17.0.16%2B8/OpenJDK17U-jdk_x64_linux_hotspot_17.0.16_8.tar.gz"
)
CMDLINE_URL = (
    "https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip"
)
SDK_PACKAGES = ("platforms;android-35", "build-tools;35.0.0", "platform-tools")


def _java_home() -> Path | None:
    env = (os.environ.get("JAVA_HOME") or "").strip()
    if env and (Path(env) / "bin" / ("java.exe" if os.name == "nt" else "java")).exists():
        return Path(env)
    if os.name == "nt" and (WIN_JBR / "bin" / "java.exe").exists():
        return WIN_JBR
    if (LINUX_JDK / "bin" / "java").exists():
        return LINUX_JDK
    return None


def _sdk_root() -> Path:
    env = (os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT") or "").strip()
    if env:
        return Path(env)
    if os.name == "nt":
        return WIN_SDK
    return LINUX_SDK


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"downloading {url}")
    result = subprocess.run(
        ["curl", "-fL", "--retry", "3", "-A", "InkCaretakerBuild/1.0", "-o", str(dest), url],
        check=False,
    )
    if result.returncode != 0 or not dest.exists() or dest.stat().st_size < 1_000_000:
        dest.unlink(missing_ok=True)
        raise SystemExit(f"download failed: {url}")


def _ensure_jdk() -> Path:
    existing = _java_home()
    if existing is not None:
        return existing
    if os.name == "nt":
        raise SystemExit("Android Studio JBR not found; install Android Studio or set JAVA_HOME")

    archive = Path.home() / ".cache" / "ink-apk" / "jdk-17.tar.gz"
    if not archive.exists():
        _download(JDK_URL, archive)
    staging = Path.home() / ".cache" / "ink-apk" / "jdk-extract"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    subprocess.run(["tar", "-xzf", str(archive), "-C", str(staging)], check=True)
    unpacked = next(p for p in staging.iterdir() if p.is_dir())
    LINUX_JDK.parent.mkdir(parents=True, exist_ok=True)
    if LINUX_JDK.exists():
        shutil.rmtree(LINUX_JDK)
    shutil.move(str(unpacked), str(LINUX_JDK))
    shutil.rmtree(staging, ignore_errors=True)
    print(f"jdk    {LINUX_JDK}")
    return LINUX_JDK


def _sdkmanager(sdk: Path) -> Path:
    latest = sdk / "cmdline-tools" / "latest" / "bin" / "sdkmanager"
    if latest.exists():
        return latest
    return sdk / "cmdline-tools" / "bin" / "sdkmanager"


def _ensure_sdk(java_home: Path) -> Path:
    sdk = _sdk_root()
    manager = _sdkmanager(sdk)
    if not manager.exists():
        archive = Path.home() / ".cache" / "ink-apk" / "cmdline-tools.zip"
        if not archive.exists():
            _download(CMDLINE_URL, archive)
        staging = Path.home() / ".cache" / "ink-apk" / "cmdline-extract"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(staging)
        target = sdk / "cmdline-tools" / "latest"
        if target.exists():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        extracted = staging / "cmdline-tools"
        shutil.move(str(extracted), str(target))
        shutil.rmtree(staging, ignore_errors=True)
        manager = target / "bin" / "sdkmanager"
        manager.chmod(manager.stat().st_mode | stat.S_IEXEC)
        print(f"sdk    {sdk}")

    env = {
        **os.environ,
        "JAVA_HOME": str(java_home),
        "ANDROID_HOME": str(sdk),
        "ANDROID_SDK_ROOT": str(sdk),
    }
    print("accepting Android SDK licenses")
    subprocess.run(
        [str(manager), f"--sdk_root={sdk}", "--licenses"],
        input="y\n" * 80,
        text=True,
        env=env,
        check=False,
    )
    print("installing", " ".join(SDK_PACKAGES))
    result = subprocess.run(
        [str(manager), f"--sdk_root={sdk}", "--install", *SDK_PACKAGES],
        env=env,
    )
    if result.returncode != 0:
        raise SystemExit(result.returncode)
    return sdk


def main() -> int:
    java_home = _ensure_jdk()
    sdk = _ensure_sdk(java_home)
    (ANDROID / "local.properties").write_text(f"sdk.dir={sdk.as_posix()}\n", encoding="utf-8")

    wrapper = ANDROID / ("gradlew.bat" if os.name == "nt" else "gradlew")
    if os.name != "nt":
        wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)

    env = {
        **os.environ,
        "JAVA_HOME": str(java_home),
        "ANDROID_HOME": str(sdk),
        "ANDROID_SDK_ROOT": str(sdk),
    }
    cmd = [str(wrapper), "assembleDebug", "--no-daemon"]
    print("building", " ".join(cmd))
    result = subprocess.run(cmd, cwd=ANDROID, env=env)
    if result.returncode != 0:
        return result.returncode
    apk = ANDROID / "app" / "build" / "outputs" / "apk" / "debug" / "app-debug.apk"
    print(f"apk  {apk}")
    return 0 if apk.exists() else 1


if __name__ == "__main__":
    raise SystemExit(main())
