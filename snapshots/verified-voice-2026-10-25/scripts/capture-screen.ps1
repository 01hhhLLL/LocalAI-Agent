param(
    [Parameter(Mandatory = $true)]
    [ValidateRange(512, 8192)]
    [int]$MaxImageSide,
    [long]$ForegroundWindowHandle = 0
)

$ErrorActionPreference = 'Stop'
$utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $utf8

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type -TypeDefinition @'
using System.Runtime.InteropServices;
 [StructLayout(LayoutKind.Sequential)]
 public struct LocalAIPoint { public int X; public int Y; }
 [StructLayout(LayoutKind.Sequential)]
 public struct LocalAIRect { public int Left; public int Top; public int Right; public int Bottom; }
public static class LocalAIVisionDpi {
    [DllImport("user32.dll")]
    public static extern bool SetProcessDPIAware();
    [DllImport("user32.dll")]
    public static extern bool SetForegroundWindow(System.IntPtr hwnd);
    [DllImport("user32.dll")]
    public static extern System.IntPtr GetForegroundWindow();
    [DllImport("user32.dll")]
    public static extern int GetWindowLongW(System.IntPtr hwnd, int index);
    [DllImport("user32.dll")]
    public static extern bool GetWindowRect(System.IntPtr hwnd, out LocalAIRect bounds);
    [DllImport("user32.dll")]
    public static extern System.IntPtr WindowFromPoint(LocalAIPoint point);
    [DllImport("user32.dll")]
    public static extern System.IntPtr GetAncestor(System.IntPtr hwnd, uint flags);
}
'@
[void][LocalAIVisionDpi]::SetProcessDPIAware()

if ($ForegroundWindowHandle -ne 0) {
    $targetWindow = [System.IntPtr]::new($ForegroundWindowHandle)
    [void][LocalAIVisionDpi]::SetForegroundWindow($targetWindow)
    Start-Sleep -Milliseconds 80
    if ([LocalAIVisionDpi]::GetForegroundWindow() -ne $targetWindow) {
        throw 'The approved window lost focus before capture'
    }
    $targetBounds = New-Object LocalAIRect
    if (-not [LocalAIVisionDpi]::GetWindowRect($targetWindow, [ref]$targetBounds)) {
        throw 'Could not verify the approved window bounds'
    }
    $center = New-Object LocalAIPoint
    $center.X = [int](($targetBounds.Left + $targetBounds.Right) / 2)
    $center.Y = [int](($targetBounds.Top + $targetBounds.Bottom) / 2)
    $windowAtCenter = [LocalAIVisionDpi]::GetAncestor(
        [LocalAIVisionDpi]::WindowFromPoint($center), 2)
    if ($windowAtCenter -ne $targetWindow) {
        throw 'The approved window is obscured at its center'
    }
}

$source = $null
$output = $null
$graphics = $null
$stream = $null
try {
    $bounds = [System.Windows.Forms.SystemInformation]::VirtualScreen
    if ($bounds.Width -le 0 -or $bounds.Height -le 0) {
        throw 'Windows returned an invalid virtual-screen size'
    }
    $source = New-Object System.Drawing.Bitmap($bounds.Width, $bounds.Height)
    $graphics = [System.Drawing.Graphics]::FromImage($source)
    $graphics.CopyFromScreen(
        $bounds.Left, $bounds.Top, 0, 0, $source.Size,
        [System.Drawing.CopyPixelOperation]::SourceCopy
    )
    if ($ForegroundWindowHandle -ne 0) {
        $windowAfterCapture = [LocalAIVisionDpi]::GetAncestor(
            [LocalAIVisionDpi]::WindowFromPoint($center), 2)
        if ([LocalAIVisionDpi]::GetForegroundWindow() -ne $targetWindow -or
            $windowAfterCapture -ne $targetWindow) {
            throw 'The approved window lost visibility during capture'
        }
    }

    $scale = [Math]::Min(1.0, $MaxImageSide / [double][Math]::Max($source.Width, $source.Height))
    $targetWidth = [Math]::Max(1, [int][Math]::Round($source.Width * $scale))
    $targetHeight = [Math]::Max(1, [int][Math]::Round($source.Height * $scale))
    if ($targetWidth -eq $source.Width -and $targetHeight -eq $source.Height) {
        $output = $source
        $source = $null
    } else {
        $output = New-Object System.Drawing.Bitmap($targetWidth, $targetHeight)
        $resize = [System.Drawing.Graphics]::FromImage($output)
        try {
            $resize.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
            $resize.DrawImage($source, 0, 0, $targetWidth, $targetHeight)
        } finally {
            $resize.Dispose()
        }
    }

    $stream = New-Object System.IO.MemoryStream
    $output.Save($stream, [System.Drawing.Imaging.ImageFormat]::Png)
    $result = @{
        screen_left = $bounds.Left
        screen_top = $bounds.Top
        screen_width = $bounds.Width
        screen_height = $bounds.Height
        image_width = $output.Width
        image_height = $output.Height
        mime_type = 'image/png'
        image_base64 = [Convert]::ToBase64String($stream.ToArray())
        foreground_window_handle = [LocalAIVisionDpi]::GetForegroundWindow().ToInt64()
        foreground_window_rect = if ($ForegroundWindowHandle -ne 0) {
            @($targetBounds.Left, $targetBounds.Top, $targetBounds.Right, $targetBounds.Bottom)
        } else { $null }
    }
    [Console]::Out.Write(($result | ConvertTo-Json -Compress -Depth 3))
} finally {
    if ($stream) { $stream.Dispose() }
    if ($graphics) { $graphics.Dispose() }
    if ($output) { $output.Dispose() }
    if ($source) { $source.Dispose() }
}
