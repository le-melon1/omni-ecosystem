#!/bin/bash
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

ACTIVE_SMART="auto/chat"
ACTIVE_LIGHT="gemini/gemini-3-flash-preview"
ACTIVE_URL="http://127.0.0.1:9099"

# Подтягиваем актуальную модель из active_pool.json если файл обновлялся
if [ -f "$DIR/active_pool.json" ]; then
    D_SMART=$(python3 -c "import json; print(json.load(open('$DIR/active_pool.json')).get('smart_model',''))" 2>/dev/null)
    D_LIGHT=$(python3 -c "import json; print(json.load(open('$DIR/active_pool.json')).get('light_model',''))" 2>/dev/null)
    D_URL=$(python3 -c "import json; print(json.load(open('$DIR/active_pool.json')).get('proxy_url',''))" 2>/dev/null)
    [ -n "$D_SMART" ] && ACTIVE_SMART="$D_SMART"
    [ -n "$D_LIGHT" ] && ACTIVE_LIGHT="$D_LIGHT"
    [ -n "$D_URL" ] && ACTIVE_URL="$D_URL"
fi

export ANTHROPIC_BASE_URL="$ACTIVE_URL"
export ANTHROPIC_AUTH_TOKEN="sk-9e61b4bb8a128d44-5c456d-dc8deecf"
export ANTHROPIC_API_KEY="sk-9e61b4bb8a128d44-5c456d-dc8deecf"
export ANTHROPIC_MODEL="$ACTIVE_SMART"
export ANTHROPIC_SMALL_MODEL="$ACTIVE_LIGHT"
export ANTHROPIC_SMALL_FAST_MODEL="$ACTIVE_LIGHT"
export ANTHROPIC_DEFAULT_OPUS_MODEL="$ACTIVE_SMART"
export ANTHROPIC_DEFAULT_SONNET_MODEL="$ACTIVE_SMART"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="$ACTIVE_LIGHT"

echo ""
echo "=========================================================================="
echo "🚀 Claude Code запущен через OmniRoute Failover Proxy"
echo "🧠 Smart-модель : $ACTIVE_SMART"
echo "⚡ Light-модель : $ACTIVE_LIGHT"
echo "🔗 Прокси       : $ACTIVE_URL"
echo "=========================================================================="
echo ""

# Флаг --model передается явно, чтобы Claude Code не брал старую закешированную модель
exec claude --model "$ACTIVE_SMART" "$@"
