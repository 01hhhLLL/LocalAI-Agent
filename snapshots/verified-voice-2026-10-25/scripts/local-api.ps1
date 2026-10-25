$ErrorActionPreference = 'Stop'
$env:PYTHONIOENCODING = 'utf-8'
$env:HF_HUB_OFFLINE = '1'
$env:TRANSFORMERS_OFFLINE = '1'
$projectRoot = Split-Path -Parent $PSScriptRoot
$env:HF_HOME = Join-Path $projectRoot 'cache\huggingface'
$env:TORCH_HOME = Join-Path $projectRoot 'cache\torch'
$env:CUDA_VISIBLE_DEVICES = '0'
$python = Join-Path $projectRoot 'runtime\fast\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw "Project Python was not found: $python"
}
try {
    $health = Invoke-RestMethod -Uri 'http://127.0.0.1:8765/healthz' -TimeoutSec 2
    if ($health.status -eq 'ready') {
        Write-Output 'Local Qwen API is already ready at 127.0.0.1:8765.'
        exit 0
    }
} catch {
    # No responding API: continue to the explicit foreground start below.
}
$entry = Join-Path $projectRoot 'services\local_openai_api.py'
& $python -B $entry
exit $LASTEXITCODE
