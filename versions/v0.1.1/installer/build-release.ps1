param()

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$payload = Join-Path $PSScriptRoot 'payload.zip'
$out = Join-Path $root 'dist\windows-x64'
if (-not (Get-Command dotnet -ErrorAction SilentlyContinue)) {
    throw 'The .NET 8 SDK is required to build the Windows preview installer.'
}
& python (Join-Path $PSScriptRoot 'build_payload.py')
if ($LASTEXITCODE -ne 0) { throw 'Payload build failed.' }
& dotnet publish (Join-Path $PSScriptRoot 'LocalAIAgent.Setup.csproj') `
    -c Release -r win-x64 --self-contained true -o $out `
    -p:PublishSingleFile=true -p:EnableCompressionInSingleFile=true
if ($LASTEXITCODE -ne 0) { throw 'Windows installer build failed.' }
$exe = Join-Path $out 'LocalAI-Agent.exe'
if (-not (Test-Path -LiteralPath $exe)) { throw 'Installer executable is missing.' }
Get-FileHash -Algorithm SHA256 -LiteralPath $exe | Select-Object Path,Hash
