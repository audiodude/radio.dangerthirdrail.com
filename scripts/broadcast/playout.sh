#!/usr/bin/env bash
# Window-gated countdown -> ident -> show. Python owns the live-start handshake
# and feeds one persistent stream-copy FFmpeg connection.
set -euo pipefail
exec python3 "$(dirname "$0")/playout.py"
