param(
    [ValidateSet('start', 'status', 'stop')]
    [string]$Action = 'start'
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
. (Join-Path $PSScriptRoot 'environment.ps1')
$env:PYTHONIOENCODING = 'utf-8'
$projectRoot = Split-Path -Parent $PSScriptRoot
$entry = Join-Path $projectRoot 'services\voice_hotkey.py'
$pythonw = Join-Path $projectRoot 'runtime\asr\Scripts\pythonw.exe'
$statusFile = Join-Path $projectRoot 'runtime\voice-hotkey\status.json'

function Get-VoiceHotkeyStatus {
    if (-not (Test-Path -LiteralPath $statusFile)) { return $null }
    try {
        $item = Get-Content -LiteralPath $statusFile -Raw -Encoding UTF8 | ConvertFrom-Json
        $process = Get-Process -Id ([int]$item.pid) -ErrorAction SilentlyContinue
        if ($null -eq $process -or $process.HasExited) { return $null }
        $basePythonwPattern = Join-Path $projectRoot 'runtime\python\*\pythonw.exe'
        if ($process.Path -ne $pythonw -and $process.Path -notlike $basePythonwPattern) { return $null }
        if ($item.state -eq 'stopped') { return $null }
        return $item
    } catch {
        return $null
    }
}

if ($Action -eq 'status') {
    $current = Get-VoiceHotkeyStatus
    if ($null -eq $current) {
        Write-Output 'Voice hotkey is not running.'
        exit 1
    }
    Write-Output ("Ctrl+Alt+X is active; state: {0}; process: {1}" -f $current.state, $current.pid)
    exit 0
}

if ($Action -eq 'stop') {
    $current = Get-VoiceHotkeyStatus
    if ($null -eq $current) {
        Write-Output 'Voice hotkey is not running.'
        exit 0
    }
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class LocalAiHotkeyMessage {
    [DllImport("user32.dll", SetLastError = true)]
    public static extern bool PostThreadMessage(uint threadId, uint message,
        UIntPtr wParam, IntPtr lParam);
}
'@
    $posted = [LocalAiHotkeyMessage]::PostThreadMessage(
        [uint32]$current.thread_id, [uint32]0x0312,
        [UIntPtr]::new(0x4C42), [IntPtr]::Zero)
    if (-not $posted) { throw 'Could not ask the voice hotkey to exit.' }
    for ($attempt = 0; $attempt -lt 50; $attempt++) {
        Start-Sleep -Milliseconds 100
        if ($null -eq (Get-VoiceHotkeyStatus)) {
            Write-Output 'Voice hotkey stopped.'
            exit 0
        }
    }
    throw 'Voice hotkey did not stop within five seconds.'
}

$current = Get-VoiceHotkeyStatus
if ($null -ne $current) {
    Write-Output ("Ctrl+Alt+X is already active; process: {0}" -f $current.pid)
    exit 0
}
if (-not (Test-Path -LiteralPath $pythonw) -or -not (Test-Path -LiteralPath $entry)) {
    throw 'The project voice hotkey runtime is incomplete.'
}
$nonce = [guid]::NewGuid().ToString('N')
$env:LOCALAI_VOICE_HOTKEY_NONCE = $nonce
try {
    $process = Start-Process -FilePath $pythonw -ArgumentList @('-B', $entry) `
        -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru
} finally {
    Remove-Item Env:\LOCALAI_VOICE_HOTKEY_NONCE -ErrorAction SilentlyContinue
}
for ($attempt = 0; $attempt -lt 100; $attempt++) {
    Start-Sleep -Milliseconds 100
    $ready = Get-VoiceHotkeyStatus
    if ($null -ne $ready -and $ready.start_nonce -eq $nonce) {
        Write-Output ("Ctrl+Alt+X is active; process: {0}. Press again to stop recording; Ctrl+Alt+Shift+X exits." -f $ready.pid)
        exit 0
    }
}
throw 'Voice hotkey did not become ready within ten seconds.'
