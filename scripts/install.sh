#!/bin/sh
set -eu
repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
python3 -m venv "$repo_dir/.venv"
"$repo_dir/.venv/bin/pip" install -e "$repo_dir"
mkdir -p "$HOME/.local/bin"
ln -sfn "$repo_dir/.venv/bin/pmc" "$HOME/.local/bin/pmc"
"$repo_dir/.venv/bin/pmc" init-config
echo "Installed pmc at $HOME/.local/bin/pmc"
