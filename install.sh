#!/usr/bin/env bash
set -e

# Claude Code Multi-Provider Proxy Installer (macOS/Linux)
# --------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0;37m' # No Color

echo -e "${BLUE}=== Starting Claude Code Proxy Setup ===${NC}"

# Check for python
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}Error: python3 is required but not installed.${NC}"
    exit 1
fi

# Install uv if missing
if ! command -v uv &> /dev/null; then
    echo -e "${BLUE}Installing uv package manager...${NC}"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

# Ensure Python 3.14 is installed/used if available
echo -e "${BLUE}Setting up Python environment...${NC}"
if uv python list | grep -q "3.14.0"; then
    echo "Python 3.14.0 already available"
else
    echo "Installing Python 3.14.0 via uv..."
    uv python install 3.14.0 || true
fi

# Build and install package locally
echo -e "${BLUE}Installing free-claude-code package...${NC}"
uv tool install --force --editable .

# Setup config directories
echo -e "${BLUE}Configuring environment templates...${NC}"
mkdir -p "$HOME/.fcc/profiles"
mkdir -p "$HOME/.fcc/logs"

if [ ! -f "$HOME/.fcc/.env" ]; then
    cp .env.example "$HOME/.fcc/.env"
    echo -e "${GREEN}Created new config at ~/.fcc/.env. Edit this file to add your API keys.${NC}"
else
    echo "Config file already exists at ~/.fcc/.env (skipping)"
fi

echo ""
echo -e "${GREEN}=== Setup Completed Successfully! ===${NC}"
echo -e "To start the local proxy server:"
echo -e "  ${BLUE}fcc-server${NC}"
echo ""
echo -e "Open the Admin UI dashboard at:"
echo -e "  ${BLUE}http://127.0.0.1:8082/admin${NC}"
echo ""
echo -e "To launch Claude Code using this proxy:"
echo -e "  ${BLUE}fcc-claude${NC}"
echo "--------------------------------------------------------"
