#!/bin/bash
# Double-click this file in Finder to install NeuralNexus MCP.
# It opens in Terminal, walks you through a one-time setup (API key + folder
# to share), and installs a background service that starts on login.
set -euo pipefail
cd "$(dirname "$0")"

echo "================================================"
echo "  NeuralNexus MCP — one-click install"
echo "================================================"
echo
echo "This will set up NeuralNexus MCP on your Mac."
echo "You'll need your NeuralNexus API key (starts with sk-)."
echo

./scripts/install.sh

echo
echo "All done! You can close this window."
