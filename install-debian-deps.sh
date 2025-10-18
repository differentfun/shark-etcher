#!/usr/bin/env bash
set -euo pipefail

if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    source /etc/os-release
    if [[ "${ID:-}" != "debian" ]]; then
        printf 'This installer targets Debian. Detected ID=%s (%s).\n' "${ID:-unknown}" "${PRETTY_NAME:-unknown}" >&2
        exit 1
    fi
else
    echo "Unable to read /etc/os-release; cannot verify the distribution." >&2
    exit 1
fi

APT_GET=$(command -v apt-get || true)
if [[ -z "${APT_GET}" ]]; then
    echo "apt-get not found. This script requires an apt-based system." >&2
    exit 1
fi

if [[ -n "${VERSION_ID:-}" && "${VERSION_ID%%.*}" -lt 13 ]]; then
    printf 'Warning: Detected Debian version %s. This script was tested against Debian 13.\n' "${VERSION_ID}" >&2
fi

PACKAGES=(
    python3
    python3-tk
    python3-pip
    python3-venv
    policykit-1
    udisks2
)

if [[ "${EUID}" -ne 0 ]]; then
    if command -v sudo >/dev/null 2>&1; then
        SUDO=(sudo)
    else
        echo "Please run this script as root or install sudo first." >&2
        exit 1
    fi
else
    SUDO=()
fi

echo "Updating apt package index..."
"${SUDO[@]}" apt-get update

echo "Installing Shark Etcher dependencies: ${PACKAGES[*]}"
DEBIAN_FRONTEND=noninteractive "${SUDO[@]}" apt-get install -y --no-install-recommends "${PACKAGES[@]}"

echo "Verifying that tkinter is available..."
if python3 - <<'PY' >/dev/null 2>&1
import tkinter
PY
then
    echo "tkinter import succeeded."
else
    echo "Warning: tkinter import failed. Please verify the installation manually." >&2
fi

echo "Done. You can now launch Shark Etcher with ./shark-etcher.sh or python3 -m shark_etcher."
