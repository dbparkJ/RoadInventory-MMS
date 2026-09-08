[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $BootstrapArgs
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonSetup = Join-Path $PSScriptRoot "setup.ps1"
$webRoot = Join-Path $projectRoot "webui"
$venvDir = ".venv"
for ($index = 0; $index -lt $BootstrapArgs.Count; $index++) {
    if ($BootstrapArgs[$index] -eq "--venv-dir" -and $index + 1 -lt $BootstrapArgs.Count) {
        $venvDir = $BootstrapArgs[++$index]
    } elseif ($BootstrapArgs[$index] -like "--venv-dir=*") {
        $venvDir = $BootstrapArgs[$index].Substring(11)
    } elseif ($BootstrapArgs[$index] -eq "--project-root" -and $index + 1 -lt $BootstrapArgs.Count) {
        $projectRoot = $BootstrapArgs[++$index]
    } elseif ($BootstrapArgs[$index] -like "--project-root=*") {
        $projectRoot = $BootstrapArgs[$index].Substring(15)
    }
}
$projectRoot = [System.IO.Path]::GetFullPath($projectRoot)
$webRoot = Join-Path $projectRoot "webui"
if (-not [System.IO.Path]::IsPathRooted($venvDir)) {
    $venvDir = Join-Path $projectRoot $venvDir
}
$venvPython = Join-Path $venvDir "Scripts\python.exe"
$verifyScript = Join-Path $PSScriptRoot "build_web.py"
$dryRun = $BootstrapArgs -contains "--dry-run"

Write-Host "[web-setup] Configuring the Python/CUDA environment."
& powershell -NoProfile -ExecutionPolicy Bypass -File $pythonSetup @BootstrapArgs
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
if ($dryRun) {
    Write-Host "[web-setup] PLAN npm ci --no-audit --no-fund"
    Write-Host "[web-setup] PLAN npm run build"
    Write-Host "[web-setup] PLAN Python scripts/build_web.py verify (also required without npm)"
    exit 0
}

$npm = Get-Command npm.cmd -CommandType Application -ErrorAction SilentlyContinue |
    Select-Object -First 1
if ($null -eq $npm) {
    Write-Host "[web-setup] npm unavailable; verifying packaged source and UI assets."
    & $venvPython $verifyScript verify --project-root $projectRoot
    exit $LASTEXITCODE
}

Push-Location $webRoot
$previousBuildPython = $env:MMS_BUILD_PYTHON
try {
    $env:MMS_BUILD_PYTHON = $venvPython
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
    & $venvPython $verifyScript verify --project-root $projectRoot
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}
finally {
    $env:MMS_BUILD_PYTHON = $previousBuildPython
    Pop-Location
}

Write-Host "[web-setup] Web workspace setup is complete."
