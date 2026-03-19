#!/usr/bin/env bash
# install.sh — Hollow dependency installer and setup launcher
# Installs: uv, gh CLI, Ollama (optional), Python deps
# Then runs the interactive setup wizard.

set -euo pipefail

HOLLOW_ROOT="$(cd "$(dirname "$0")" && pwd)"

# ── Colors ──────────────────────────────────────────────────────────────────

green()  { printf '\033[92m%s\033[0m\n' "$*"; }
yellow() { printf '\033[93m%s\033[0m\n' "$*"; }
red()    { printf '\033[91m%s\033[0m\n' "$*"; }
cyan()   { printf '\033[96m%s\033[0m\n' "$*"; }
dim()    { printf '\033[2m%s\033[0m\n' "$*"; }
bold()   { printf '\033[1m%s\033[0m\n' "$*"; }

# ── OS detection ─────────────────────────────────────────────────────────────

detect_os() {
    if [[ "$OSTYPE" == "darwin"* ]]; then
        echo "macos"
    elif [[ -f /etc/debian_version ]]; then
        echo "debian"
    elif [[ -f /etc/redhat-release ]]; then
        echo "redhat"
    else
        echo "unknown"
    fi
}

OS=$(detect_os)

# ── Helpers ──────────────────────────────────────────────────────────────────

confirm() {
    local msg="$1"
    local default="${2:-y}"
    if [[ "$default" == "y" ]]; then
        printf '%s [Y/n]: ' "$msg"
    else
        printf '%s [y/N]: ' "$msg"
    fi
    read -r reply
    reply="${reply:-$default}"
    [[ "$reply" =~ ^[Yy]$ ]]
}

require_cmd() {
    local cmd="$1"
    if ! command -v "$cmd" &>/dev/null; then
        red "Required command not found: $cmd"
        return 1
    fi
}

# ── Step 1: Python 3.11+ ─────────────────────────────────────────────────────

check_python() {
    cyan "Checking Python..."

    local python_cmd=""
    for cmd in python3.14 python3.13 python3.12 python3.11 python3; do
        if command -v "$cmd" &>/dev/null; then
            local ver
            ver=$("$cmd" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
            local major minor
            major=$(echo "$ver" | cut -d. -f1)
            minor=$(echo "$ver" | cut -d. -f2)
            if [[ "$major" -ge 3 && "$minor" -ge 11 ]]; then
                python_cmd="$cmd"
                break
            fi
        fi
    done

    if [[ -z "$python_cmd" ]]; then
        red "Python 3.11+ is required but not found."
        echo ""
        echo "Install it:"
        case "$OS" in
            macos)   echo "  brew install python@3.11" ;;
            debian)  echo "  sudo apt install python3.11" ;;
            redhat)  echo "  sudo dnf install python3.11" ;;
            *)       echo "  https://www.python.org/downloads/" ;;
        esac
        exit 1
    fi

    green "  Python $ver found ($python_cmd)"
    PYTHON_CMD="$python_cmd"
}

# ── Step 2: uv ───────────────────────────────────────────────────────────────

install_uv() {
    cyan "Checking uv..."
    if command -v uv &>/dev/null; then
        local ver
        ver=$(uv --version 2>/dev/null | head -1)
        green "  uv already installed ($ver)"
        return
    fi

    yellow "  uv not found — installing..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # Reload PATH for this script session
    export PATH="$HOME/.cargo/bin:$HOME/.local/bin:$PATH"
    if command -v uv &>/dev/null; then
        green "  uv installed"
    else
        red "  uv install failed. Install manually: https://docs.astral.sh/uv/getting-started/installation/"
        exit 1
    fi
}

# ── Step 3: gh CLI ────────────────────────────────────────────────────────────

install_gh() {
    cyan "Checking gh CLI..."
    if command -v gh &>/dev/null; then
        local ver
        ver=$(gh --version 2>/dev/null | head -1)
        green "  gh already installed ($ver)"
        return
    fi

    if confirm "  gh CLI not found. Install it? (enables GitHub operations from agents)"; then
        case "$OS" in
            macos)
                if command -v brew &>/dev/null; then
                    brew install gh
                else
                    yellow "  Homebrew not found. Install gh manually: https://cli.github.com"
                fi
                ;;
            debian)
                curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
                    | sudo dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg 2>/dev/null
                sudo chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg
                echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
                    | sudo tee /etc/apt/sources.list.d/github-cli.list > /dev/null
                sudo apt update -q && sudo apt install -y gh
                ;;
            redhat)
                sudo dnf install -y gh 2>/dev/null || {
                    yellow "  dnf install failed. Install gh manually: https://cli.github.com"
                }
                ;;
            *)
                yellow "  Unsupported OS for auto-install. Install gh manually: https://cli.github.com"
                ;;
        esac
        if command -v gh &>/dev/null; then
            green "  gh installed"
            dim "  Run 'gh auth login' to authenticate after setup."
        fi
    else
        dim "  Skipping gh CLI (can be installed later)"
    fi
}

# ── Step 4: Ollama (optional — for local embeddings) ─────────────────────────

install_ollama() {
    cyan "Checking Ollama (local vector embeddings)..."
    if command -v ollama &>/dev/null; then
        green "  Ollama already installed"
        # Pull model if not present
        if ! ollama list 2>/dev/null | grep -q "nomic-embed-text"; then
            if confirm "  Pull nomic-embed-text embedding model (~274MB)?"; then
                ollama pull nomic-embed-text
                green "  nomic-embed-text ready"
            else
                dim "  Skipping model pull (can run: ollama pull nomic-embed-text later)"
            fi
        else
            green "  nomic-embed-text model already present"
        fi
        return
    fi

    echo ""
    dim "  Ollama enables local semantic memory (no external API)."
    dim "  Without it, Hollow falls back to keyword-only memory — still functional."
    echo ""
    if confirm "  Install Ollama for local embeddings? (recommended)"; then
        curl -fsSL https://ollama.com/install.sh | sh
        if command -v ollama &>/dev/null; then
            green "  Ollama installed"
            # Start the server in background so we can pull models
            ollama serve &>/dev/null &
            sleep 2
            if confirm "  Pull nomic-embed-text embedding model (~274MB)?"; then
                ollama pull nomic-embed-text
                green "  nomic-embed-text ready"
            fi
        else
            yellow "  Ollama install may have failed. Check: https://ollama.com"
        fi
    else
        dim "  Skipping Ollama. Hollow will use keyword-only memory."
        dim "  Install later: curl -fsSL https://ollama.com/install.sh | sh"
        dim "  Then: ollama pull nomic-embed-text"
    fi
}

# ── Step 5: Python dependencies ───────────────────────────────────────────────

install_python_deps() {
    cyan "Installing Python dependencies..."
    cd "$HOLLOW_ROOT"
    uv sync
    green "  Python dependencies installed"
}

# ── Step 6: claude CLI check ──────────────────────────────────────────────────

check_claude() {
    cyan "Checking claude CLI..."
    if command -v claude &>/dev/null; then
        green "  claude CLI found"
    else
        yellow "  claude CLI not found."
        echo ""
        echo "  Hollow requires the Claude Code CLI with a Claude MAX subscription."
        echo "  Install: https://claude.ai/code"
        echo "  After install, run: claude  (to authenticate)"
        echo ""
        if ! confirm "  Continue setup anyway? (you must install claude CLI before running Hollow)"; then
            echo ""
            echo "Install claude CLI, then re-run: ./install.sh"
            exit 1
        fi
    fi
}

# ── Main ──────────────────────────────────────────────────────────────────────

main() {
    echo ""
    bold "Hollow — Installer"
    cyan "================================================================"
    echo ""
    dim "This script will install dependencies and launch the setup wizard."
    echo ""

    check_python
    install_uv
    install_gh
    install_ollama
    install_python_deps
    check_claude

    echo ""
    cyan "================================================================"
    green "Dependencies ready. Launching setup wizard..."
    cyan "================================================================"
    echo ""

    cd "$HOLLOW_ROOT"
    uv run python setup.py
}

main "$@"
