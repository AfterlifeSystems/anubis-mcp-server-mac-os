#!/bin/bash
# Double-click this file in Finder to remove NeuralNexus MCP from your Mac.
set -euo pipefail
cd "$(dirname "$0")"

echo "================================================"
echo "  NeuralNexus MCP — uninstall"
echo "================================================"
echo
read -r -p "Also delete saved settings and login credentials? [y/N]: " answer
case "$answer" in
  y | Y | yes | YES)
    ./scripts/uninstall.sh --purge
    ;;
  *)
    ./scripts/uninstall.sh
    ;;
esac

echo
echo "Done. You can close this window."
