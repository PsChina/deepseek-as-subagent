#!/usr/bin/env bash
# Compatibility endpoint for the retired remote-pipe installer.

set -euo pipefail

cat >&2 <<'EOF'
✗ The curl|bash installer has been retired for supply-chain safety.

This script never downloads, updates, or executes repository code. Install from
an explicitly reviewed revision instead:

  git clone https://github.com/PsChina/deepseek-as-subagent.git
  cd deepseek-as-subagent
  # Inspect install.sh and requirements.lock, then run:
  ./install.sh

For Codex, run `bash adapters/codex/install.sh` from that same reviewed checkout.
EOF

exit 1
