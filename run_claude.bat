@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
chcp 65001 >nul

set "ACTIVE_SMART=agy/gemini-3.8-flash-high"
set "ACTIVE_LIGHT=gemini/gemini-3.1-flash-lite"
set "ACTIVE_URL=http://127.0.0.1:9099"

if exist "%~dp0active_pool.json" (
    for /f "delims=" %%i in ('python -c "import json; print(json.load(open('active_pool.json')).get('smart_model',''))" 2^>nul') do if not "%%i"=="" set "ACTIVE_SMART=%%i"
    for /f "delims=" %%i in ('python -c "import json; print(json.load(open('active_pool.json')).get('light_model',''))" 2^>nul') do if not "%%i"=="" set "ACTIVE_LIGHT=%%i"
    for /f "delims=" %%i in ('python -c "import json; print(json.load(open('active_pool.json')).get('proxy_url',''))" 2^>nul') do if not "%%i"=="" set "ACTIVE_URL=%%i"
)

set ANTHROPIC_BASE_URL=!ACTIVE_URL!
set ANTHROPIC_AUTH_TOKEN=sk-9e61b4bb8a128d44-5c456d-dc8deecf
set ANTHROPIC_API_KEY=sk-9e61b4bb8a128d44-5c456d-dc8deecf
set ANTHROPIC_MODEL=!ACTIVE_SMART!
set ANTHROPIC_SMALL_MODEL=!ACTIVE_LIGHT!
set ANTHROPIC_SMALL_FAST_MODEL=!ACTIVE_LIGHT!
set ANTHROPIC_DEFAULT_OPUS_MODEL=!ACTIVE_SMART!
set ANTHROPIC_DEFAULT_SONNET_MODEL=!ACTIVE_SMART!
set ANTHROPIC_DEFAULT_HAIKU_MODEL=!ACTIVE_LIGHT!

echo.
echo ==========================================================================
echo 🚀 Claude Code zapuschen cherez OmniRoute Failover Proxy
echo 🧠 Aktivnaya Smart-model : !ACTIVE_SMART!
echo ⚡ Aktivnaya Light-model : !ACTIVE_LIGHT!
echo 🔗 Proxy-adres          : !ACTIVE_URL!
echo ==========================================================================
echo.

claude --model "!ACTIVE_SMART!" %*
