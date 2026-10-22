$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$LocalPython = Join-Path $ProjectDir ".build-venv\Scripts\python.exe"
$BundledPython = "C:\Users\67697\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"

if (Test-Path -LiteralPath $LocalPython) {
    $Python = $LocalPython
} elseif (Test-Path -LiteralPath $BundledPython) {
    $Python = $BundledPython
} else {
    throw "No Python build environment found."
}

Set-Location -LiteralPath $ProjectDir
& $Python -m PyInstaller --noconfirm --clean --windowed --onefile `
    --name "MultiAIInput" `
    --add-data "web;web" `
    --collect-all playwright `
    --hidden-import playwright.async_api `
    app.py

Write-Host "Build complete: $ProjectDir\dist\MultiAIInput.exe"
