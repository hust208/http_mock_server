#!/bin/bash
cd "$(dirname "$0")"
PORT=${1:-5000}
if command -v python3 &>/dev/null; then PYTHON=python3; elif command -v python &>/dev/null; then PYTHON=python; else echo "Error: Python 3 not found"; exit 1; fi
if ! $PYTHON -c "import flask" 2>/dev/null; then echo "Installing deps..."; pip3 install -r requirements.txt || pip install -r requirements.txt; fi
export MOCK_PORT=$PORT
echo "Starting HTTP Mock Server on port $PORT..."
exec $PYTHON app.py
