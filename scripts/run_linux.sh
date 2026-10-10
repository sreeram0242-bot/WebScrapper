#!/usr/bin/env bash
# WebScrapper - Universal Silent Launcher for Linux/macOS
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ "$(basename "$SCRIPT_DIR")" = "scripts" ]; then
    SCRIPT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
fi
cd "$SCRIPT_DIR"

case "$(uname -s)" in
    CYGWIN*|MINGW*|MSYS*)
        cmd.exe /c start wscript.exe //nologo "scripts\\webscarpper.vbs"
        exit 0
        ;;
esac

# Find Python 3
PYTHON_BIN=""
if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="python3"
elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="python"
fi

if [ -z "$PYTHON_BIN" ]; then
    echo "Python 3 is required. Please install Python 3.9+."
    exit 1
fi

VENV_DIR="$SCRIPT_DIR/venv"
if [ ! -f "$VENV_DIR/bin/python" ] || ! "$VENV_DIR/bin/python" -c "import sys" >/dev/null 2>&1; then
    rm -rf "$VENV_DIR"
    "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

if ! "$VENV_DIR/bin/python" -c "import bs4, selenium, webdriver_manager, pandas, flask, flask_cors" >/dev/null 2>&1; then
    "$VENV_DIR/bin/python" -m pip install -r "$SCRIPT_DIR/requirements.txt" >/dev/null 2>&1
fi

# Run in background invisibly
nohup "$VENV_DIR/bin/python" "$SCRIPT_DIR/app.py" > "$SCRIPT_DIR/output/scraper_server.log" 2>&1 &

# Wait for server and open browser
for i in {1..20}; do
    if curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:5000 2>/dev/null | grep -q "200"; then
        break
    fi
    sleep 0.5
done

if command -v open >/dev/null 2>&1; then
    open "http://127.0.0.1:5000"
elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "http://127.0.0.1:5000"
fi
