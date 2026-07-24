#!/usr/bin/env sh

set -u

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
probe="import platform,struct,sys; raise SystemExit(0 if platform.python_implementation() == 'CPython' and sys.version_info[:2] == (3, 12) and struct.calcsize('P') * 8 == 64 else 1)"
explicit_python=
expect_python_path=false

for argument
do
    if [ "$expect_python_path" = true ]; then
        explicit_python=$argument
        break
    fi
    case "$argument" in
        --python)
            expect_python_path=true
            ;;
        --python=*)
            explicit_python=${argument#--python=}
            break
            ;;
    esac
done

if [ -n "$explicit_python" ]; then
    if [ -x "$explicit_python" ] &&
        "$explicit_python" -c "$probe" >/dev/null 2>&1; then
        exec "$explicit_python" "$script_dir/bootstrap_environment.py" "$@"
    fi
    printf 'The interpreter passed with --python is not runnable 64-bit CPython 3.12: %s\n' \
        "$explicit_python" >&2
    exit 2
fi

for candidate in python3.12 python3 python; do
    if ! command -v "$candidate" >/dev/null 2>&1; then
        continue
    fi
    if "$candidate" -c "$probe" >/dev/null 2>&1; then
        exec "$candidate" "$script_dir/bootstrap_environment.py" "$@"
    fi
done

printf '%s\n' \
    "64-bit CPython 3.12 was not found in this terminal." \
    "Install Python 3.12 and its venv module, reopen the terminal, and run:" \
    "  bash scripts/setup.sh" \
    "This launcher does not install operating-system packages automatically." >&2
exit 2
