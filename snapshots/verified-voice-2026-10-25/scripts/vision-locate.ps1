param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Arguments
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
. (Join-Path $PSScriptRoot 'environment.ps1')
$python = Join-Path $env:UV_PROJECT_ENVIRONMENT 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw "Project Python was not found: $python"
}
$entry = Join-Path $PSScriptRoot 'vision-locate.py'
& $python -B $entry @Arguments
exit $LASTEXITCODE
