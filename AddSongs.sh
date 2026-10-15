#!/bin/bash
# Usage: ./AddSongs.sh "<spotify playlist link>"

DIR="$(cd "$(dirname "$0")" && pwd)"

# Set up the Python environment on first run
if [ ! -x "$DIR/.venv/bin/python" ]; then
    python3 -m venv "$DIR/.venv" && "$DIR/.venv/bin/pip" install -q -r "$DIR/requirements.txt" || exit 1
fi

PYTHONPATH="$DIR" exec "$DIR/.venv/bin/python" -m jseeker "$@"
