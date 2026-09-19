"""Start the server and print how to reach it from the tablet.

    python run.py

The tablet cannot use localhost, so this resolves the LAN address of this
machine and renders it as a QR code you can point the tablet's camera at.
"""

from __future__ import annotations

import socket
import subprocess
import sys

import uvicorn

from server import config

FIREWALL_RULE = "InkPipeline"


def lan_ip() -> str | None:
    """The address other devices on this Wi-Fi use to reach this machine.

    Opening a UDP socket to an external address makes the OS pick the interface
    it would actually route through; nothing is sent.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        return probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()


def firewall_rule_exists() -> bool:
    if not sys.platform.startswith("win"):
        return True
    try:
        result = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule", f"name={FIREWALL_RULE}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def console_handles_block_characters() -> bool:
    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        "\u2588\u2580\u2584".encode(encoding)
        return True
    except (LookupError, UnicodeEncodeError):
        return False


def qr(url: str) -> None:
    """Render the URL as a scannable QR code in the terminal.

    print_ascii packs two rows per line using half-block characters, which a
    console on a legacy codepage renders as mojibake. In that case fall back to
    two spaces per module, which is bulkier but scans just as well.
    """
    try:
        import qrcode

        code = qrcode.QRCode(border=2)
        code.add_data(url)
        code.make(fit=True)

        if console_handles_block_characters():
            code.print_ascii(invert=True)
            return

        for row in code.get_matrix():
            print("".join("  " if module else "##" for module in row))
    except Exception as exc:  # noqa: BLE001 - a missing QR must not stop startup
        print(f"  (QR code unavailable: {exc})")


def main() -> None:
    # Tolerate characters the console cannot represent instead of dying on
    # them. The encoding itself is left alone so the QR renderer below can see
    # what this console can actually display.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

    ip = lan_ip()
    tablet_url = f"http://{ip}:{config.PORT}/canvas" if ip else None

    print()
    print("  Ink pipeline")
    print(f"  captures -> {config.CAPTURE_DIR}")
    print(f"  idle capture after {config.IDLE_TIMEOUT_MS / 1000:g}s of no drawing")
    print()
    print(f"  this machine   http://localhost:{config.PORT}/viewer   (optional)")

    if tablet_url:
        print(f"  tablet         {tablet_url}")
        print()
        qr(tablet_url)
    else:
        print("  tablet         could not detect a LAN address; are you on Wi-Fi?")

    if not firewall_rule_exists():
        print("  The tablet will be blocked until Windows Firewall allows the port.")
        print("  Run this once in an *administrator* PowerShell:")
        print()
        print(
            f'    New-NetFirewallRule -DisplayName "{FIREWALL_RULE}" -Direction Inbound '
            f"-Action Allow -Protocol TCP -LocalPort {config.PORT} -Profile Private"
        )
    print()
    sys.stdout.flush()

    uvicorn.run("server.app:app", host=config.HOST, port=config.PORT, log_level="warning")


if __name__ == "__main__":
    main()
