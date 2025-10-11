#!/usr/bin/env bash
set -euo pipefail

echo "Removing Shark Etcher menu shortcut..."

APPLICATIONS_DIR="${XDG_DATA_HOME:-${HOME}/.local/share}/applications"
DESKTOP_FILE="${APPLICATIONS_DIR}/shark-etcher.desktop"

if [[ -f "${DESKTOP_FILE}" ]]; then
    rm -f "${DESKTOP_FILE}"
    echo "Shortcut removed from ${DESKTOP_FILE}"
else
    echo "No shortcut found at ${DESKTOP_FILE}"
fi
