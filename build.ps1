<#
.SYNOPSIS
  Build ShowTVDownloader into a one-folder Windows-service .exe with PyInstaller.

.DESCRIPTION
  Produces dist\ShowTVDownloader\ShowTVDownloader.exe (+ _internal\).
  Run from the project root:   .\build.ps1
#>
[CmdletBinding()]
param(
    [switch]$Clean
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
Set-Location $root

# Prefer the project venv's python if present.
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

Write-Host "==> Using Python: $py"
& $py --version

Write-Host "==> Installing build + runtime dependencies..."
& $py -m pip install --disable-pip-version-check -q -r requirements.txt
& $py -m pip install --disable-pip-version-check -q -r requirements-build.txt

if ($Clean) {
    Write-Host "==> Cleaning previous build output..."
    Remove-Item -Recurse -Force (Join-Path $root "build") -ErrorAction SilentlyContinue
    Remove-Item -Recurse -Force (Join-Path $root "dist") -ErrorAction SilentlyContinue
}

Write-Host "==> Running PyInstaller..."
& $py -m PyInstaller --noconfirm "ShowTVDownloader.spec"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed (exit $LASTEXITCODE)" }

$exe = Join-Path $root "dist\ShowTVDownloader\ShowTVDownloader.exe"
if (-not (Test-Path $exe)) { throw "Expected exe not found at $exe" }

$ver = (& $py -c "import version; print(version.__version__)").Trim()
Write-Host ""
Write-Host "==> Build OK (v$ver)" -ForegroundColor Green
Write-Host "    $exe"
Write-Host ""
Write-Host "Next steps (run an *elevated* PowerShell):"
Write-Host "    cd `"$root\dist\ShowTVDownloader`""
Write-Host "    .\ShowTVDownloader.exe install"
Write-Host "    .\ShowTVDownloader.exe start"
Write-Host "    # then open http://localhost:5000"
