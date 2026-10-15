#!/usr/bin/env bash
# Двойной клик на Mac открывает это окно терминала
cd "$(dirname "$0")"
clear
echo "==================================================="
echo "   Запуск OmniRoute + OpenClaw Proxy (macOS)       "
echo "==================================================="
echo ""
python3 omni_openclaw.py "$@"
