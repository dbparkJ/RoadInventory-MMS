[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $BootstrapArgs
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonSetup = Join-Path $PSScriptRoot "setup.ps1"
$webRoot = Join-Path $projectRoot "webui"
$builtIndex = Join-Path $webRoot "dist\index.html"
$dryRun = $BootstrapArgs -contains "--dry-run"

Write-Host "[web-setup] Configuring the Python/CUDA environment."
& powershell -NoProfile -ExecutionPolicy Bypass -File $pythonSetup @BootstrapArgs
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
if ($dryRun) {
    Write-Host "[web-setup] PLAN npm ci --no-audit --no-fund"
    Write-Host "[web-setup] PLAN npm run build"
    exit 0
}

$npm = Get-Command npm.cmd -CommandType Application -ErrorAction SilentlyContinue |
    Select-Object -First 1
if ($null -eq $npm) {
    if (Test-Path $builtIndex) {
        Write-Host (
            "[web-setup] Node.js was not found, but the committed UI build " +
            "is ready for server runtime."
        )
        exit 0
    }
    Write-Error (
        "Node.js/npm and a built UI were not found. " +
        "Install Node.js LTS and run this script again."
    )
    exit 2
}

Push-Location $webRoot
try {
    Write-Host "[web-setup] Installing locked frontend packages."
    & $npm.Source ci --no-audit --no-fund
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
    Write-Host "[web-setup] Building the production UI."
    & $npm.Source run build
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}
finally {
    Pop-Location
}

Write-Host "[web-setup] Web workspace setup is complete."
