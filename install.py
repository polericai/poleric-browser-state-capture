#!/usr/bin/env python3
"""Cross-platform bootstrap installer for poleric-browser-state-capture.

Works on macOS/Linux/Windows using the same invocation pattern:
  python install.py

It installs/updates pipx, installs the CLI package, and installs
Playwright Chromium browser binaries.
"""

from __future__ import annotations

import os
import platform
import shlex
import subprocess
import sys

PACKAGE_NAME = "poleric-browser-state-capture"
GIT_FALLBACK_SPEC = "git+https://github.com/polericai/poleric-browser-state-capture.git"


def log(msg: str) -> None:
    print(f"[poleric-installer] {msg}")


def run(cmd: list[str], *, check: bool = True) -> int:
    printable = " ".join(shlex.quote(part) for part in cmd)
    log(f"$ {printable}")
    result = subprocess.run(cmd)
    if check and result.returncode != 0:
        raise SystemExit(result.returncode)
    return result.returncode


def ensure_python_version() -> None:
    if sys.version_info < (3, 10):
        raise SystemExit("Python 3.10+ is required.")


def ensure_pipx() -> None:
    log("Installing/upgrading pipx in user scope...")
    run([sys.executable, "-m", "pip", "install", "--user", "--upgrade", "pipx"])

    # Keep this best-effort. If it cannot modify shell profile, we still continue
    # by invoking pipx through `python -m pipx`.
    run([sys.executable, "-m", "pipx", "ensurepath"], check=False)


def install_or_upgrade_tool() -> None:
    log(f"Installing/upgrading {PACKAGE_NAME} with pipx...")
    rc = run([sys.executable, "-m", "pipx", "install", "--force", PACKAGE_NAME], check=False)
    if rc == 0:
        return

    log("PyPI install failed. Falling back to GitHub source...")
    run([sys.executable, "-m", "pipx", "install", "--force", GIT_FALLBACK_SPEC])


def install_playwright_chromium() -> None:
    log("Installing Playwright Chromium browser binaries...")
    run([sys.executable, "-m", "pipx", "run", "--spec", "playwright", "playwright", "install", "chromium"])


def print_done() -> None:
    log("Setup complete.")
    log("Run the tool with: poleric-state-capture --help")
    log("If command not found in current terminal, open a new terminal window.")


def main() -> None:
    log(f"OS: {platform.system()} {platform.release()}")
    log(f"Python: {sys.version.split()[0]}")

    # Allow package override for testing/release candidates.
    pkg_override = os.getenv("POLERIC_PACKAGE_NAME", "").strip()
    global PACKAGE_NAME
    if pkg_override:
        PACKAGE_NAME = pkg_override
        log(f"Using package override: {PACKAGE_NAME}")

    ensure_python_version()
    ensure_pipx()
    install_or_upgrade_tool()
    install_playwright_chromium()
    print_done()


if __name__ == "__main__":
    main()
