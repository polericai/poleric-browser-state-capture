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

pipx_cli() {
  if command -v pipx >/dev/null 2>&1; then
    pipx "$@"
    return $?
  fi

  if run_py -m pipx --version >/dev/null 2>&1; then
    run_py -m pipx "$@"
    return $?
  fi

  return 1
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

install_pipx_with_system_manager() {
  if command -v brew >/dev/null 2>&1; then
    log "Installing pipx with Homebrew..."
    brew install pipx || brew upgrade pipx || true
    return 0
  fi

  if command -v apt-get >/dev/null 2>&1; then
    log "Installing pipx with apt-get..."
    if [ "$(id -u)" -eq 0 ]; then
      apt-get update -y && apt-get install -y pipx
    elif command -v sudo >/dev/null 2>&1; then
      sudo apt-get update -y && sudo apt-get install -y pipx
    else
      return 1
    fi
    return 0
  fi

  if command -v dnf >/dev/null 2>&1; then
    log "Installing pipx with dnf..."
    if [ "$(id -u)" -eq 0 ]; then
      dnf install -y pipx
    elif command -v sudo >/dev/null 2>&1; then
      sudo dnf install -y pipx
    else
      return 1
    fi
    return 0
  fi

  if command -v yum >/dev/null 2>&1; then
    log "Installing pipx with yum..."
    if [ "$(id -u)" -eq 0 ]; then
      yum install -y pipx
    elif command -v sudo >/dev/null 2>&1; then
      sudo yum install -y pipx
    else
      return 1
    fi
    return 0
  fi

  if command -v pacman >/dev/null 2>&1; then
    log "Installing pipx with pacman..."
    if [ "$(id -u)" -eq 0 ]; then
      pacman -Sy --noconfirm pipx
    elif command -v sudo >/dev/null 2>&1; then
      sudo pacman -Sy --noconfirm pipx
    else
      return 1
    fi
    return 0
  fi

  return 1
}

install_or_upgrade_pipx() {
  if pipx_cli --version >/dev/null 2>&1; then
    log "pipx already available."
    pipx_cli ensurepath || true
    return 0
  fi

  log "pipx not found. Attempting system package manager install first..."
  install_pipx_with_system_manager || true

  if pipx_cli --version >/dev/null 2>&1; then
    pipx_cli ensurepath || true
    return 0
  fi

  log "System package manager path failed. Attempting Python user install for pipx..."
  if run_py -m pip install --user --upgrade pipx; then
    pipx_cli ensurepath || true
    return 0
  fi

  log "Unable to install pipx automatically on this system."
  log "Please install pipx manually, then rerun this installer."
  exit 1
}

install_or_upgrade_tool() {
  log "Installing/upgrading $PACKAGE_NAME via pipx..."

  if pipx_cli install --force "$PACKAGE_NAME"; then
    return 0
  fi

  log "PyPI install failed. Falling back to GitHub source..."
  pipx_cli install --force "$GIT_FALLBACK_SPEC"
}

install_playwright_browsers() {
  log "Installing Playwright Chromium browser binaries..."
  pipx_cli run --spec playwright playwright install chromium
}

print_next_steps() {
  cat <<'MSG'

[poleric-installer] Setup complete.
[poleric-installer] Open a new terminal (or reload shell) and run:
  poleric-state-capture --help

If command is still not found, run:
  pipx ensurepath
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
