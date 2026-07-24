[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $BootstrapArgs
)

$bootstrapScript = Join-Path $PSScriptRoot "bootstrap_environment.py"
$probe = "import platform,struct,sys; raise SystemExit(0 if platform.python_implementation() == 'CPython' and sys.version_info[:2] == (3, 12) and struct.calcsize('P') * 8 == 64 else 1)"
$explicitPython = $null
for ($index = 0; $index -lt $BootstrapArgs.Count; $index++) {
    if ($BootstrapArgs[$index] -eq "--python" -and $index + 1 -lt $BootstrapArgs.Count) {
        $explicitPython = $BootstrapArgs[$index + 1]
        break
    }
}

$candidates = @()
if ($null -ne $explicitPython) {
    $candidates += @{ Name = $explicitPython; Prefix = @(); Explicit = $true }
}
$candidates += @(
    @{ Name = "py"; Prefix = @("-3.12"); Explicit = $false },
    @{ Name = "python3.12"; Prefix = @(); Explicit = $false },
    @{ Name = "python"; Prefix = @(); Explicit = $false },
    @{ Name = "python3"; Prefix = @(); Explicit = $false }
)

foreach ($candidate in $candidates) {
    $command = Get-Command $candidate.Name -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -eq $command) {
        if ($candidate.Explicit) {
            Write-Error "The interpreter passed with --python was not found: $explicitPython"
            exit 2
        }
        continue
    }

    $probeArgs = @($candidate.Prefix) + @("-c", $probe)
    & $command.Source @probeArgs *> $null
    if ($LASTEXITCODE -ne 0) {
        if ($candidate.Explicit) {
            Write-Error (
                "The interpreter passed with --python is not runnable 64-bit CPython 3.12: " +
                $explicitPython
            )
            exit 2
        }
        continue
    }

    $runArgs = @($candidate.Prefix) + @($bootstrapScript) + @($BootstrapArgs)
    & $command.Source @runArgs
    exit $LASTEXITCODE
}

Write-Error (
    "64-bit CPython 3.12 was not found in this terminal. " +
    "Install Python 3.12, reopen the terminal, and run scripts\setup.ps1 again. " +
    "This launcher does not install operating-system packages automatically."
)
exit 2
