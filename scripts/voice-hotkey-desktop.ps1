param()

$ErrorActionPreference = 'Stop'
$controller = Join-Path $PSScriptRoot 'voice-hotkey.ps1'
$powershell = Join-Path $PSHOME 'powershell.exe'

try {
    $started = Start-Process -FilePath $powershell -ArgumentList @(
        '-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
        '-WindowStyle', 'Hidden', '-File', ('"' + $controller + '"'),
        '-Action', 'start'
    ) -WindowStyle Hidden -Wait -PassThru
    if ($started.ExitCode -ne 0) {
        throw "Voice hotkey startup failed (exit code $($started.ExitCode))."
    }
    $message = "语音助手已就绪。`n按 Ctrl+Alt+X 开始录音，再按一次结束。"
    $title = '本地语音助手'
    $style = 64
} catch {
    $message = "语音助手未能启动。`n请检查项目运行环境。"
    $title = '本地语音助手：启动失败'
    $style = 16
}

$popup = New-Object -ComObject WScript.Shell
[void]$popup.Popup($message, 5, $title, $style)
