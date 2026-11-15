#!/bin/sh
set -eu
task_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$task_root"
python3 -m unittest discover -s tests -v
python3 scripts/package.py --validate-only
python3 scripts/check_screen_ui.py
