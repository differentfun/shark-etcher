#!/usr/bin/env bash
set -euo pipefail

echo "Creating Shark Etcher menu shortcut..."

SCRIPT_DIR="$(cd -- "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_EXEC="${SCRIPT_DIR}/shark-etcher.sh"

if [[ ! -x "${APP_EXEC}" ]]; then
    echo "Error: ${APP_EXEC} is not executable or missing." >&2
    exit 1
fi

APPLICATIONS_DIR="${XDG_DATA_HOME:-${HOME}/.local/share}/applications"
DESKTOP_FILE="${APPLICATIONS_DIR}/shark-etcher.desktop"

mkdir -p "${APPLICATIONS_DIR}"

cat > "${DESKTOP_FILE}" <<EOF
[Desktop Entry]
Type=Application
Name=Shark Etcher
Comment=Flash disk images without telemetry
Exec=${APP_EXEC}
Icon=utilities-terminal
Terminal=false
Categories=Utility;
StartupNotify=true
EOF

chmod 644 "${DESKTOP_FILE}"

echo "Shortcut created at ${DESKTOP_FILE}"
