#!/usr/bin/env bash
set -euo pipefail

PACKAGE_NAME="${POLERIC_PACKAGE_NAME:-poleric-browser-state-capture}"
GIT_FALLBACK_SPEC="${POLERIC_GIT_FALLBACK_SPEC:-git+https://github.com/polericai/poleric-browser-state-capture.git}"

log() {
  echo "[poleric-installer] $*"
}

detect_python() {
  if command -v python3 >/dev/null 2>&1; then
    echo "python3"
    return 0
  fi
  if command -v python >/dev/null 2>&1; then
    echo "python"
    return 0
  fi

  log "Python 3 is required but was not found."
  log "Please install Python 3.10+ and run this installer again."
  exit 1
}

PYTHON_BIN="$(detect_python)"

run_py() {
  "$PYTHON_BIN" "$@"
}

ensure_pip() {
  if run_py -m pip --version >/dev/null 2>&1; then
    return 0
  fi

  log "pip not found for $PYTHON_BIN. Attempting ensurepip..."
  if ! run_py -m ensurepip --upgrade >/dev/null 2>&1; then
    log "Could not bootstrap pip automatically."
    log "Please install pip for $PYTHON_BIN and re-run."
    exit 1
  fi
}

install_or_upgrade_pipx() {
  log "Installing/upgrading pipx in user scope..."
  run_py -m pip install --user --upgrade pipx

  # Best-effort PATH update for future shells.
  run_py -m pipx ensurepath || true
}

install_or_upgrade_tool() {
  log "Installing/upgrading $PACKAGE_NAME via pipx..."

  if run_py -m pipx install --force "$PACKAGE_NAME"; then
    return 0
  fi

  log "PyPI install failed. Falling back to GitHub source..."
  run_py -m pipx install --force "$GIT_FALLBACK_SPEC"
}

install_playwright_browsers() {
  log "Installing Playwright Chromium browser binaries..."
  run_py -m pipx run --spec playwright playwright install chromium
}

print_next_steps() {
  cat <<'MSG'

[poleric-installer] Setup complete.
[poleric-installer] Open a new terminal (or reload shell) and run:
  poleric-state-capture --help

If command is still not found, use:
  python3 -m pipx ensurepath
(or)
  python -m pipx ensurepath
then restart terminal.
MSG
}

main() {
  log "Using Python: $PYTHON_BIN"

  ensure_pip
  install_or_upgrade_pipx
  install_or_upgrade_tool
  install_playwright_browsers
  print_next_steps
}

main "$@"
