#!/usr/bin/env bash
set -e

echo "============================================"
echo " Discord Music Bot - Linux/macOS Setup"
echo "============================================"
echo

# Check Python
if ! command -v python3 &>/dev/null; then
    echo "[ERROR] Python 3 is not installed."
    echo "        Install via: sudo apt install python3 python3-venv python3-pip"
    exit 1
fi
echo "[OK] Python 3 found: $(python3 --version)"

# Check ffmpeg
if ! command -v ffmpeg &>/dev/null; then
    echo "[WARN] ffmpeg not found. Will try imageio-ffmpeg fallback."
    echo "       For best results: sudo apt install ffmpeg"
else
    echo "[OK] ffmpeg found"
fi

# Create virtual environment
if [ ! -d "venv" ]; then
    echo
    echo "[*] Creating virtual environment..."
    python3 -m venv venv
fi
echo "[OK] Virtual environment ready"

# Activate and install dependencies
echo
echo "[*] Installing dependencies..."
source venv/bin/activate
pip install --upgrade pip >/dev/null 2>&1
pip install -r requirements.txt

# Download bgutil-pot binary
if [ ! -f "bgutil-pot" ]; then
    echo
    echo "[*] Downloading bgutil-pot (YouTube PO token generator)..."
    ARCH=$(uname -m)
    OS=$(uname -s | tr '[:upper:]' '[:lower:]')
    if [ "$ARCH" = "x86_64" ] && [ "$OS" = "linux" ]; then
        BINARY_URL="https://github.com/jim60105/bgutil-ytdlp-pot-provider-rs/releases/latest/download/bgutil-pot-linux-x86_64"
    elif [ "$ARCH" = "aarch64" ] && [ "$OS" = "linux" ]; then
        BINARY_URL="https://github.com/jim60105/bgutil-ytdlp-pot-provider-rs/releases/latest/download/bgutil-pot-linux-aarch64"
    elif [ "$OS" = "darwin" ]; then
        BINARY_URL="https://github.com/jim60105/bgutil-ytdlp-pot-provider-rs/releases/latest/download/bgutil-pot-macos-x86_64"
    else
        echo "[WARN] Unsupported platform ($OS $ARCH). Skipping bgutil-pot download."
        BINARY_URL=""
    fi

    if [ -n "$BINARY_URL" ]; then
        curl -fsSL -o bgutil-pot "$BINARY_URL" && chmod +x bgutil-pot
        if [ $? -eq 0 ]; then
            echo "[OK] bgutil-pot downloaded"
        else
            echo "[WARN] Failed to download bgutil-pot. PO tokens will not work."
        fi
    fi
else
    echo "[OK] bgutil-pot already present"
fi

# Check for Deno
if ! command -v deno &>/dev/null; then
    echo
    echo "[*] Installing Deno (required for YouTube signature solving)..."
    curl -fsSL https://deno.land/install.sh | sh
    export DENO_INSTALL="$HOME/.deno"
    export PATH="$DENO_INSTALL/bin:$PATH"
    if command -v deno &>/dev/null; then
        echo "[OK] Deno installed"
    else
        echo "[WARN] Deno may not be on PATH. Add ~/.deno/bin to your PATH."
    fi
else
    echo "[OK] Deno found: $(deno --version | head -1)"
fi

# Config file
if [ ! -f "config.yaml" ]; then
    echo
    echo "[*] Creating config.yaml from template..."
    cp config.example.yaml config.yaml
    echo "[!!] Edit config.yaml with your bot token and owner ID before running!"
fi

echo
echo "============================================"
echo " Setup complete!"
echo " 1. Edit config.yaml with your bot token"
echo " 2. Run: source venv/bin/activate && python3 main.py"
echo "============================================"
