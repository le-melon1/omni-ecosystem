$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

$activeSmart = "agy/gemini-3.8-flash-high"
$activeLight = "gemini/gemini-3.1-flash-lite"
$activeUrl = "http://127.0.0.1:9099"

if (Test-Path "$scriptDir\active_pool.json") {
    try {
        $data = Get-Content "$scriptDir\active_pool.json" -Raw | ConvertFrom-Json
        if ($data.smart_model) { $activeSmart = $data.smart_model }
        if ($data.light_model) { $activeLight = $data.light_model }
        if ($data.proxy_url) { $activeUrl = $data.proxy_url }
    } catch {}
}

$env:ANTHROPIC_BASE_URL = $activeUrl
$env:ANTHROPIC_AUTH_TOKEN = "sk-9e61b4bb8a128d44-5c456d-dc8deecf"
$env:ANTHROPIC_API_KEY = "sk-9e61b4bb8a128d44-5c456d-dc8deecf"
$env:ANTHROPIC_MODEL = $activeSmart
$env:ANTHROPIC_SMALL_MODEL = $activeLight
$env:ANTHROPIC_SMALL_FAST_MODEL = $activeLight
$env:ANTHROPIC_DEFAULT_OPUS_MODEL = $activeSmart
$env:ANTHROPIC_DEFAULT_SONNET_MODEL = $activeSmart
$env:ANTHROPIC_DEFAULT_HAIKU_MODEL = $activeLight

Write-Host "`n==========================================================================" -ForegroundColor Cyan
Write-Host "🚀 Claude Code запущен через OmniRoute Failover Proxy" -ForegroundColor Green
Write-Host "🧠 Smart-модель : $activeSmart" -ForegroundColor Yellow
Write-Host "⚡ Light-модель : $activeLight" -ForegroundColor Yellow
Write-Host "🔗 Прокси       : $activeUrl" -ForegroundColor Gray
Write-Host "==========================================================================`n" -ForegroundColor Cyan

claude --model "$activeSmart" @args
