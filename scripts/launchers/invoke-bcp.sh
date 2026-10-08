#!/usr/bin/env sh

set -u

is_supported_python() {
    "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' \
        >/dev/null 2>&1
}

script_directory=$(
    CDPATH= cd -P "$(dirname "$0")" 2>/dev/null && pwd
) || {
    printf '%s\n' 'Não foi possível determinar a pasta do launcher BCP.' >&2
    exit 127
}

entry_point="$script_directory/../../bcp_bronze.py"

if [ ! -f "$entry_point" ]; then
    printf '%s\n' 'Não foi possível localizar a entrada da CLI do BulkFlow.' >&2
    exit 127
fi

if [ -n "${BCP_PYTHON:-}" ]; then
    if ! command -v "$BCP_PYTHON" >/dev/null 2>&1; then
        printf '%s\n' 'Não foi possível localizar o interpretador Python configurado em BCP_PYTHON.' >&2
        exit 127
    fi
    python_command=$BCP_PYTHON
elif command -v python3 >/dev/null 2>&1 && is_supported_python python3; then
    python_command=python3
elif command -v python >/dev/null 2>&1 && is_supported_python python; then
    python_command=python
else
    printf '%s\n' 'Não foi possível localizar o Python 3.10 ou superior. Instale-o ou defina BCP_PYTHON.' >&2
    exit 127
fi

exec "$python_command" "$entry_point" "$@"
