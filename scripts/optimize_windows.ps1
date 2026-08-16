<#
.SYNOPSIS
    optimize_windows.ps1 — Windows comprehensive maintenance (DanTech Studio).

.DESCRIPTION
    Performs: temporary files cleanup, system integrity repair (SFC/DISM),
    network stack reset (winsock/ip/flushdns) and software updates (winget).
    Requires an elevated PowerShell session; self-elevates via UAC when needed.

.PARAMETER SkipSystemRepair
    Skips SFC /scannow and DISM /RestoreHealth (faster runs).

.PARAMETER SkipNetworkReset
    Skips the network stack reset (netsh winsock reset, netsh int ip reset,
    ipconfig /flushdns).

.PARAMETER SkipWinget
    Skips `winget upgrade --all`.

.PARAMETER LogPath
    Where the maintenance log is appended. Defaults to
    $env:TEMP\DanTechStudio-maintenance.log.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File optimize_windows.ps1
#>

[CmdletBinding()]
param(
    [switch]$SkipSystemRepair,
    [switch]$SkipNetworkReset,
    [switch]$SkipWinget,
    [string]$LogPath = (Join-Path $env:TEMP "DanTechStudio-maintenance.log")
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------
function Write-Step {
    param([string]$Message)
    $line = "[{0:yyyy-MM-dd HH:mm:ss}] {1}" -f (Get-Date), $Message
    Write-Host $line -ForegroundColor Cyan
    Add-Content -LiteralPath $LogPath -Value $line -Encoding UTF8
}

function Write-ErrorLog {
    param([string]$Message)
    $line = "[{0:yyyy-MM-dd HH:mm:ss}] ERROR: {1}" -f (Get-Date), $Message
    Write-Host $line -ForegroundColor Red
    Add-Content -LiteralPath $LogPath -Value $line -Encoding UTF8
}

# ---------------------------------------------------------------------------
# Privilege check / self-elevation
# ---------------------------------------------------------------------------
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $isAdmin) {
    Write-Host "Requesting Administrator privileges (UAC)..." -ForegroundColor Yellow
    try {
        $script = $MyInvocation.MyCommand.Path
        $argsLine = ($PSBoundParameters.GetEnumerator() | ForEach-Object {
            if ($_.Value -is [switch]) { "-$($_.Key)" } else { "-$($_.Key) `"$($_.Value)`"" }
        }) -join " "
        Start-Process powershell.exe -Verb RunAs -ArgumentList @(
            "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$script`"", $argsLine
        ) -Wait
        exit $LASTEXITCODE
    } catch {
        Write-ErrorLog "UAC elevation cancelled or failed: $($_.Exception.Message)"
        exit 1
    }
}

Write-Step "DanTech Studio maintenance started (elevated)."
Write-Step "Log: $LogPath"

# ---------------------------------------------------------------------------
# 1. Temporary files cleanup
# ---------------------------------------------------------------------------
Write-Step "Cleaning temporary files..."
$tempTargets = @(
    (Join-Path $env:TEMP "*"),
    (Join-Path $env:SystemRoot "Temp\*"),
    (Join-Path $env:SystemRoot "Prefetch\*")
)
$totalBytes = 0L
$deletedCount = 0
foreach ($target in $tempTargets) {
    try {
        Get-ChildItem -LiteralPath $target -Force -ErrorAction SilentlyContinue | ForEach-Object {
            try {
                $size = 0L
                if ($_.PSIsContainer) {
                    $size = (Get-ChildItem -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue |
                        Measure-Object -Property Length -Sum -ErrorAction SilentlyContinue).Sum
                } else {
                    $size = $_.Length
                }
                Remove-Item -LiteralPath $_.FullName -Recurse -Force -ErrorAction SilentlyContinue
                if ($size) { $totalBytes += $size }
                $deletedCount++
            } catch {
                # Locked files are expected; keep going.
            }
        }
    } catch {
        Write-ErrorLog "Temp cleanup failed for $target : $($_.Exception.Message)"
    }
}
Write-Step ("Temp cleanup done: {0} items, ~{1:N1} MB freed." -f $deletedCount, ($totalBytes / 1MB))

# ---------------------------------------------------------------------------
# 2. System integrity repair (SFC / DISM)
# ---------------------------------------------------------------------------
if (-not $SkipSystemRepair) {
    Write-Step "Running SFC /scannow (this can take 10-30 minutes)..."
    try {
        sfc /scannow
        Write-Step "SFC finished with exit code $LASTEXITCODE."
    } catch {
        Write-ErrorLog "SFC failed: $($_.Exception.Message)"
    }

    Write-Step "Running DISM /RestoreHealth (this can take 10-30 minutes)..."
    try {
        DISM /Online /Cleanup-Image /RestoreHealth
        Write-Step "DISM finished with exit code $LASTEXITCODE."
    } catch {
        Write-ErrorLog "DISM failed: $($_.Exception.Message)"
    }
} else {
    Write-Step "System repair skipped (-SkipSystemRepair)."
}

# ---------------------------------------------------------------------------
# 3. Network stack reset
# ---------------------------------------------------------------------------
if (-not $SkipNetworkReset) {
    Write-Step "Resetting network stack..."
    foreach ($command in @(
        @{ Name = "Winsock reset";      Args = @("winsock", "reset") },
        @{ Name = "TCP/IP stack reset"; Args = @("int", "ip", "reset") },
        @{ Name = "DNS cache flush";    Args = @("/flushdns") }
    )) {
        try {
            & netsh @($command.Args) 2>&1 | Out-Null
            Write-Step ("Network: {0} done." -f $command.Name)
        } catch {
            Write-ErrorLog ("Network: {0} failed: {1}" -f $command.Name, $_.Exception.Message)
        }
    }
    try {
        & ipconfig /flushdns 2>&1 | Out-Null
        Write-Step "DNS cache flushed."
    } catch {
        Write-ErrorLog "DNS flush failed: $($_.Exception.Message)"
    }
} else {
    Write-Step "Network reset skipped (-SkipNetworkReset)."
}

# ---------------------------------------------------------------------------
# 4. Software updates (winget)
# ---------------------------------------------------------------------------
if (-not $SkipWinget) {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Step "Updating software with winget (this can take a long time)..."
        try {
            winget upgrade --all --silent --accept-package-agreements --accept-source-agreements
            Write-Step "winget upgrade finished with exit code $LASTEXITCODE."
        } catch {
            Write-ErrorLog "winget upgrade failed: $($_.Exception.Message)"
        }
    } else {
        Write-ErrorLog "winget not found; skipping software updates."
    }
} else {
    Write-Step "Software updates skipped (-SkipWinget)."
}

Write-Step "DanTech Studio maintenance finished."