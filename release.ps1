# Ships a new Jarvis version:
#   1. bump APP_VERSION in app/jarvis_core/config.py and write installer/release-notes.md
#   2. powershell -ExecutionPolicy Bypass -File release.ps1
#   3. open the draft on GitHub and click "Publish release" -> every installed Jarvis offers the update
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$version = (Select-String -Path (Join-Path $root "app\jarvis_core\config.py") -Pattern 'APP_VERSION = "([^"]+)"').Matches[0].Groups[1].Value
$gh = (Get-Command gh -ErrorAction SilentlyContinue).Source
if (-not $gh) { $gh = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Recurse -Filter gh.exe | Select-Object -First 1 -ExpandProperty FullName }

& (Join-Path $root "build.ps1")
& $gh release create "v$version" (Join-Path $root "dist\Jarvis-Setup.exe") --draft --title "Jarvis v$version" `
    --notes-file (Join-Path $root "installer\release-notes.md") -R ZytrixDeveloper/JarvisProject
if ($LASTEXITCODE -ne 0) { throw "Creating the release failed" }
Write-Host "Draft release v$version created. Publish it at https://github.com/ZytrixDeveloper/JarvisProject/releases"
