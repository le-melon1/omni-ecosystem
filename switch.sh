#!/usr/bin/env bash
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

if [ -z "$2" ]; then
    echo "Использование: ./switch.sh [smart|light] [имя_модели]"
    echo "Пример: ./switch.sh smart grok-cli/grok-4.7-xhigh"
    echo "Пример: ./switch.sh light agy/gemini-3.8-flash-low"
    exit 1
fi

python3 omni_claude.py switch "$1" "$2"
