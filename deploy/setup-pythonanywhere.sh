#!/usr/bin/env bash
# Run in a PythonAnywhere Bash console after cloning the repository to ~/Kaiser.
set -euo pipefail
cd "$HOME/Kaiser"

runtime=""
for candidate in python3.13 python3.12; do
  if command -v "$candidate" >/dev/null 2>&1; then
    runtime="$candidate"
    break
  fi
done
if [[ -z "$runtime" ]]; then
  echo "This app requires Python 3.12 or newer. Select a current PythonAnywhere system image."
  exit 1
fi
if [[ ! -d .venv ]]; then
  "$runtime" -m venv .venv
fi
.venv/bin/python -c 'import sys; assert sys.version_info >= (3,12), "Use a Python 3.12+ virtual environment"'
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip check
.venv/bin/python -m unittest -v

umask 077
mkdir -p "$HOME/.local/share/quote-desk" "$HOME/.config/quote-desk"
chmod 700 "$HOME/.local/share/quote-desk" "$HOME/.config/quote-desk"
if [[ ! -e "$HOME/.config/quote-desk/environment.json" ]]; then
  echo '{}' > "$HOME/.config/quote-desk/environment.json"
fi
chmod 600 "$HOME/.config/quote-desk/environment.json"

echo "Dependencies and private data directories are ready."
.venv/bin/python -c 'import sys; print("Select Python %s.%s in the Web tab." % sys.version_info[:2])'
echo "Select virtualenv: $HOME/Kaiser/.venv"
echo "Use the WSGI configuration from deploy/pythonanywhere_wsgi.py, then click Reload."
