param(
    [string]$OutputDirectory = "..\..\yolo-monitor-pet"
)

$ErrorActionPreference = "Stop"
$sourceDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$outputDirectory = [IO.Path]::GetFullPath((Join-Path $sourceDirectory $OutputDirectory))
$compiler = "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if (-not (Test-Path -LiteralPath $compiler)) {
    throw "找不到 Windows .NET Framework C# 编译器：$compiler"
}
New-Item -ItemType Directory -Force -Path $outputDirectory | Out-Null

Add-Type -AssemblyName System.Drawing
$iconPath = Join-Path $sourceDirectory "app.ico"
$bitmap = New-Object Drawing.Bitmap 64,64
$graphics = [Drawing.Graphics]::FromImage($bitmap)
$graphics.SmoothingMode = [Drawing.Drawing2D.SmoothingMode]::AntiAlias
$graphics.Clear([Drawing.Color]::Transparent)
$graphics.FillEllipse((New-Object Drawing.SolidBrush([Drawing.Color]::FromArgb(25,38,68))),6,8,52,50)
$graphics.DrawArc((New-Object Drawing.Pen([Drawing.Color]::FromArgb(90,226,220),6)),8,8,48,48,-80,300)
$graphics.FillEllipse([Drawing.Brushes]::White,21,27,7,8)
$graphics.FillEllipse([Drawing.Brushes]::White,37,27,7,8)
$handle = $bitmap.GetHicon()
$icon = [Drawing.Icon]::FromHandle($handle)
$stream = [IO.File]::Open($iconPath,[IO.FileMode]::Create)
try { $icon.Save($stream) } finally { $stream.Dispose(); $graphics.Dispose(); $bitmap.Dispose() }

$sources = Get-ChildItem -LiteralPath $sourceDirectory -Filter "*.cs" | ForEach-Object FullName
$arguments = @(
    "/nologo", "/target:winexe", "/platform:x64", "/optimize+", "/debug-", "/langversion:5",
    "/win32manifest:$sourceDirectory\app.manifest", "/win32icon:$iconPath",
    "/out:$outputDirectory\YoloMonitorPet.exe",
    "/reference:System.dll", "/reference:System.Core.dll", "/reference:System.Drawing.dll",
    "/reference:System.Windows.Forms.dll", "/reference:System.Net.Http.dll", "/reference:System.Web.Extensions.dll"
) + $sources
& $compiler $arguments
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Host "Built: $outputDirectory\YoloMonitorPet.exe"
