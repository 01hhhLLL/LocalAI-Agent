param()

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
$root = Split-Path -Parent $PSScriptRoot
$source = Join-Path $root 'assets\dragon-voice-icon-head-v1.png'
$target = Join-Path $root 'assets\dragon-voice-icon-head-v1.ico'
if (Test-Path -LiteralPath $target) { throw 'Icon already exists; choose a new versioned file name.' }

$original = [System.Drawing.Image]::FromFile($source)
$bitmap = New-Object System.Drawing.Bitmap 256,256
$graphic = [System.Drawing.Graphics]::FromImage($bitmap)
$graphic.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
$graphic.CompositingQuality = [System.Drawing.Drawing2D.CompositingQuality]::HighQuality
$graphic.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::HighQuality
$graphic.DrawImage($original, 0, 0, 256, 256)
$png = New-Object System.IO.MemoryStream
try {
    $bitmap.Save($png, [System.Drawing.Imaging.ImageFormat]::Png)
    $output = [System.IO.File]::Open($target, [System.IO.FileMode]::CreateNew)
    $writer = New-Object System.IO.BinaryWriter($output)
    try {
        $writer.Write([uint16]0)  # reserved
        $writer.Write([uint16]1)  # icon
        $writer.Write([uint16]1)  # one image
        $writer.Write([byte]0)    # 256 px
        $writer.Write([byte]0)
        $writer.Write([byte]0)    # palette
        $writer.Write([byte]0)
        $writer.Write([uint16]1)  # planes
        $writer.Write([uint16]32) # bits per pixel
        $writer.Write([uint32]$png.Length)
        $writer.Write([uint32]22)
        $writer.Write($png.ToArray())
    } finally { $writer.Dispose() }
} finally {
    $png.Dispose()
    $graphic.Dispose()
    $bitmap.Dispose()
    $original.Dispose()
}
Write-Output "Icon created: $target"
