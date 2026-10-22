param([string]$Python = "python")
$ErrorActionPreference = "Stop"
Push-Location -LiteralPath $PSScriptRoot
try {
    & $Python -m PyInstaller --noconfirm --clean --windowed --onefile --name "MultiAIInput-V1" --add-data "web;web" --collect-all playwright --hidden-import playwright.async_api app.py
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed: $LASTEXITCODE" }
    Write-Host "Build complete: $PSScriptRoot\dist\MultiAIInput-V1.exe"
} finally { Pop-Location }
