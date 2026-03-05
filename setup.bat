@echo off
setlocal enabledelayedexpansion

echo ============================================
echo  Discord Music Bot - Windows Setup
echo ============================================
echo.

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python is not installed or not on PATH.
    echo         Download from https://www.python.org/downloads/
    pause
    exit /b 1
)
echo [OK] Python found

:: Check ffmpeg
ffmpeg -version >nul 2>&1
if errorlevel 1 (
    echo [WARN] ffmpeg not found on PATH. Will try imageio-ffmpeg fallback.
    echo        For best results, install ffmpeg: https://ffmpeg.org/download.html
) else (
    echo [OK] ffmpeg found
)

:: Create virtual environment
if not exist "venv" (
    echo.
    echo [*] Creating virtual environment...
    python -m venv venv
)
echo [OK] Virtual environment ready

:: Activate and install dependencies
echo.
echo [*] Installing dependencies...
call venv\Scripts\activate.bat
pip install --upgrade pip >nul 2>&1
pip install -r requirements.txt

:: Download bgutil-pot binary
if not exist "bgutil-pot.exe" (
    echo.
    echo [*] Downloading bgutil-pot (YouTube PO token generator)...
    curl -fsSL -o bgutil-pot.exe "https://github.com/jim60105/bgutil-ytdlp-pot-provider-rs/releases/latest/download/bgutil-pot-windows-x86_64.exe"
    if errorlevel 1 (
        echo [WARN] Could not download bgutil-pot. PO tokens will not work.
    ) else (
        echo [OK] bgutil-pot downloaded
    )
) else (
    echo [OK] bgutil-pot already present
)

:: Check for Deno (needed for YouTube signature solving)
deno --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo [*] Installing Deno (required for YouTube signature solving)...
    powershell -Command "irm https://deno.land/install.ps1 | iex"
    if errorlevel 1 (
        echo [WARN] Could not install Deno automatically.
        echo        Install manually: https://deno.land/#installation
    ) else (
        echo [OK] Deno installed
    )
) else (
    echo [OK] Deno found
)

:: Config file
if not exist "config.yaml" (
    echo.
    echo [*] Creating config.yaml from template...
    copy config.example.yaml config.yaml >nul
    echo [!!] Edit config.yaml with your bot token and owner ID before running!
)

echo.
echo ============================================
echo  Setup complete!
echo  1. Edit config.yaml with your bot token
echo  2. Run: venv\Scripts\activate ^& python main.py
echo ============================================
pause
