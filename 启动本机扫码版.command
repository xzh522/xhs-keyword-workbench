#!/bin/zsh
TASK_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
exec /bin/sh "$TASK_DIR/scripts/macos_bootstrap.sh" "$@"
