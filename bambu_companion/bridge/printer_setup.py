"""One-time, local setup of the printer connection:

    python -m bambu_companion.bridge.printer_setup

Asks for the printer's IP address, serial number and LAN access code
(all three are on the printer's screen under Settings > WLAN / Device,
with LAN Only / Developer Mode switched on) and stores them with
`printer_config.save_printer_config` under `%APPDATA%\\BambuCompanion\\`.

This is typed at a prompt on your own PC on purpose. The access code is
a credential: it must never be pasted into a chat, passed through the
connector, or committed to the repo — which is why no connector tool
accepts it. The access code is not echoed as you type.
"""

from __future__ import annotations

import argparse
import getpass
import re

from .printer_config import default_config_path, load_printer_config, save_printer_config

_HOST_RE = re.compile(r"^[A-Za-z0-9.\-]{1,253}$")


def validate(host: str, serial: str, access_code: str) -> list[str]:
    """Returns a list of problems (empty = looks fine). Pure."""
    problems = []
    if not _HOST_RE.match(host or ""):
        problems.append("The address should be an IP address like 192.168.1.50 (or a host name).")
    if not (serial or "").strip() or " " in serial.strip():
        problems.append("The serial number is the long code shown on the printer, with no spaces.")
    if not (access_code or "").strip():
        problems.append("The access code is empty.")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Save the printer's LAN connection details on this PC.")
    parser.add_argument("--show", action="store_true", help="Show what is saved (access code hidden) and exit")
    args = parser.parse_args(argv)

    saved = load_printer_config()
    if args.show:
        print(f"Config file: {default_config_path()}")
        print(f"  address: {saved['host'] or '(not set)'}")
        print(f"  serial:  {'(set)' if saved['serial'] else '(not set)'}")
        print(f"  code:    {'(set)' if saved['access_code'] else '(not set)'}")
        return 0

    print("Printer connection setup — stored only on this PC.\n")
    host = input(f"Printer IP address [{saved['host']}]: ").strip() or saved["host"]
    serial = input("Printer serial number [keep saved]: ").strip() or saved["serial"]
    access_code = getpass.getpass("LAN access code (hidden) [keep saved]: ").strip() or saved["access_code"]

    problems = validate(host, serial, access_code)
    if problems:
        print("\nNot saved:")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    save_printer_config(host, serial, access_code)
    check = load_printer_config()
    if (check["host"], check["serial"], check["access_code"]) != (host, serial, access_code):
        print(f"\nCould not write {default_config_path()} — nothing was saved.")
        return 1
    print(f"\nSaved to {default_config_path()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
