#!/bin/sh
set -eu
repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
python3 -m venv "$repo_dir/.venv"
"$repo_dir/.venv/bin/pip" install -e "$repo_dir"
mkdir -p "$HOME/.local/bin"
ln -sfn "$repo_dir/.venv/bin/pmc" "$HOME/.local/bin/pmc"
ln -sfn "$repo_dir/.venv/bin/pmc-add-keys" "$HOME/.local/bin/pmc-add-keys"
ln -sfn "$repo_dir/.venv/bin/pmc-add-nvidia-key" "$HOME/.local/bin/pmc-add-nvidia-key"
ln -sfn "$repo_dir/.venv/bin/pmc-add-gemini-keys" "$HOME/.local/bin/pmc-add-gemini-keys"
ln -sfn "$repo_dir/.venv/bin/pmc-add-groq-key" "$HOME/.local/bin/pmc-add-groq-key"
"$repo_dir/.venv/bin/pmc" init-config
echo "Installed pmc at $HOME/.local/bin/pmc"
echo "Installed pmc-add-keys at $HOME/.local/bin/pmc-add-keys"
echo "Installed pmc-add-nvidia-key at $HOME/.local/bin/pmc-add-nvidia-key"
echo "Installed pmc-add-gemini-keys at $HOME/.local/bin/pmc-add-gemini-keys"
echo "Installed pmc-add-groq-key at $HOME/.local/bin/pmc-add-groq-key"
