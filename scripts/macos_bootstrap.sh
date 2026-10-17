#!/bin/sh
set -eu
TASK_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$TASK_DIR"
export PYTHONUTF8=1
for TASK_PYTHON in "$TASK_DIR/.venv/bin/python" "$(command -v python3 || true)" /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
  if [ -x "$TASK_PYTHON" ] && "$TASK_PYTHON" -c 'import sys; assert sys.version_info >= (3,10)' >/dev/null 2>&1; then
    exec "$TASK_PYTHON" "$TASK_DIR/launcher.py" "$@"
  fi
done
printf '%s\n' 'Python 3.10 or newer is required: https://www.python.org/downloads/macos/'
exit 1
