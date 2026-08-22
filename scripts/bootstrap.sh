#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

python -m venv .venv
. .venv/bin/activate
python -m pip install -e .

pmc init-config

cat <<'TXT'
PMC core installed.

Next:
  1. Edit ~/.config/poormans-code/config.toml
  2. Export/source the API keys referenced by enabled candidates
  3. In a target Git repository: pmc init-repo
  4. Run: pmc doctor

For OpenHands support, activate this venv and run:
  pip install -e '.[openhands]'
TXT
