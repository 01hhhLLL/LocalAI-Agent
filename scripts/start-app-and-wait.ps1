param(
    [Parameter(Mandatory = $true)]
    [string] $Executable,
    [ValidateRange(1, 30)]
    [int] $TimeoutSeconds = 12,
    [switch] $CheckOnly
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) {
    throw 'Executable not found'
}

$processName = [IO.Path]::GetFileNameWithoutExtension($Executable)
$existing = @(Get-Process -Name $processName -ErrorAction SilentlyContinue | Where-Object {
    try { $_.Path -ieq $Executable } catch { $false }
})
$alreadyRunning = $existing.Count -gt 0
$steamHelper = if ([IO.Path]::GetFileName($Executable) -ieq 'steam.exe') {
    Join-Path (Split-Path -Parent $Executable) 'bin\cef\cef.win64\steamwebhelper.exe'
} else { $null }
$launched = $null
if (-not $alreadyRunning -and -not $CheckOnly) {
    $launched = Start-Process -FilePath $Executable `
        -WorkingDirectory (Split-Path -Parent $Executable) -PassThru -ErrorAction Stop
}

$clock = [Diagnostics.Stopwatch]::StartNew()
$observedProcess = $null
$observedWindow = $null
do {
    $running = @(Get-Process -Name $processName -ErrorAction SilentlyContinue | Where-Object {
        try { $_.Path -ieq $Executable } catch { $false }
    })
    $observedProcess = $null
    $observedWindow = $null
    if ($running.Count -gt 0) {
        $observedProcess = $running[0]
        $observedWindow = $running | Where-Object {
            $_.MainWindowHandle -ne 0 -and $_.MainWindowTitle
        } | Select-Object -First 1
        # Steam presents login and account selection in its installed CEF helper.
        # The click layer independently verifies that helper's parent is Steam.
        if (-not $observedWindow -and $steamHelper) {
            $observedWindow = Get-Process -Name steamwebhelper -ErrorAction SilentlyContinue |
                Where-Object {
                    try { $_.Path -ieq $steamHelper -and $_.MainWindowHandle -ne 0 -and
                        $_.MainWindowTitle } catch { $false }
                } | Select-Object -First 1
        }
    }
    if ($observedWindow -or $clock.Elapsed.TotalSeconds -ge $TimeoutSeconds) { break }
    Start-Sleep -Milliseconds 300
} while ($true)

$launcherExited = $null
$launcherExitCode = $null
if ($launched) {
    $launched.Refresh()
    $launcherExited = $launched.HasExited
    if ($launcherExited) { $launcherExitCode = $launched.ExitCode }
}

$status = if ($observedWindow) {
    'window_visible'
} elseif ($observedProcess) {
    'process_running'
} else {
    'not_running'
}

[pscustomobject]@{
    status = $status
    already_running = $alreadyRunning
    launch_pid = if ($launched) { $launched.Id } else { $null }
    process_pid = if ($observedProcess) { $observedProcess.Id } else { $null }
    window_title = if ($observedWindow) { $observedWindow.MainWindowTitle } else { $null }
    launcher_exited = $launcherExited
    launcher_exit_code = $launcherExitCode
    elapsed_seconds = [Math]::Round($clock.Elapsed.TotalSeconds, 2)
} | ConvertTo-Json -Compress
