#!/bin/bash
# Start the labeler for the current user on the ganymede2 login node.
# Run it through the tunnel command in docs/connecting.md, for example from a laptop:
#   ssh -t -L 8765:localhost:8765 <netid>@ganymede2.utdallas.edu /titan/frodrigues/scint_labeler/code/cluster/serve.sh 8765
# The server stops when the SSH session ends. Labels are saved on every key press.
set -eo pipefail
ROOT=/titan/frodrigues/scint_labeler
PORT="${1:-8765}"
if (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; then
  echo
  echo "  Port $PORT is already in use on $(hostname) (maybe another labeler)."
  echo "  Close this window and run the same command with another number in BOTH places, e.g. 8766:"
  echo "    ssh -t -L 8766:localhost:8766 $USER@ganymede2.utdallas.edu $0 8766"
  echo
  exit 1
fi
if [ ! -w "$ROOT/labels" ]; then
  echo "  You cannot write to $ROOT/labels. Ask Isaac to add you to the 'uars' group." >&2
  exit 1
fi
cd "$ROOT"
exec "$ROOT/env/bin/scintlab" serve --port "$PORT"
