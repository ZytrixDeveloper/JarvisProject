# Builds Jarvis.exe (PyInstaller) and the Jarvis-Setup.exe installer (Inno Setup).
# Usage:  powershell -ExecutionPolicy Bypass -File build.ps1
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$app = Join-Path $root "app"
$py = Join-Path $app ".venv\Scripts\python.exe"

Push-Location $app
try {
    & $py assets\make_assets.py
    & $py -m PyInstaller --noconfirm --clean --windowed `
        --name Jarvis `
        --icon assets\jarvis.ico `
        --add-data "ui;ui" `
        --collect-all faster_whisper `
        --collect-all ctranslate2 `
        --collect-all openwakeword `
        --collect-all onnxruntime `
        --collect-all webview `
        --collect-all sounddevice `
        --collect-data edge_tts `
        --collect-submodules pystray `
        --collect-submodules pycaw `
        --collect-submodules comtypes `
        --collect-all screen_brightness_control `
        --hidden-import wave `
        --exclude-module tkinter `
        jarvis.py
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
} finally {
    Pop-Location
}

$iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe") |
    Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "Inno Setup not found. Install it with: winget install JRSoftware.InnoSetup" }
$version = (Select-String -Path (Join-Path $app "jarvis_core\config.py") -Pattern 'APP_VERSION = "([^"]+)"').Matches[0].Groups[1].Value
& $iscc "/DAppVersion=$version" (Join-Path $root "installer\jarvis.iss")
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }
Write-Host "Done: $(Join-Path $root 'dist\Jarvis-Setup.exe')"
