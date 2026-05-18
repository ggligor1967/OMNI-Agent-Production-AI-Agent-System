param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('dashboard', 'cli', 'telegram', 'all')]
    [string]$Mode
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$envFile = Join-Path $repoRoot '.env'

function Get-EnvSetting {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name,
        [Parameter(Mandatory = $true)]
        [AllowEmptyString()]
        [string]$Default
    )

    $envItem = Get-Item -Path ("Env:$Name") -ErrorAction SilentlyContinue
    if ($envItem -and -not [string]::IsNullOrWhiteSpace($envItem.Value)) {
        return $envItem.Value
    }

    if (-not (Test-Path $envFile)) {
        return $Default
    }

    $pattern = '^\s*' + [regex]::Escape($Name) + '\s*=\s*(.+?)\s*$'
    foreach ($line in Get-Content -Path $envFile) {
        if ([string]::IsNullOrWhiteSpace($line)) {
            continue
        }

        $trimmed = $line.TrimStart()
        if ($trimmed.StartsWith('#')) {
            continue
        }

        $match = [regex]::Match($line, $pattern)
        if ($match.Success) {
            return $match.Groups[1].Value.Trim().Trim('"').Trim("'")
        }
    }

    return $Default
}

function Get-CandidatePorts {
    param(
        [Parameter(Mandatory = $true)]
        [int]$PrimaryPort,
        [Parameter(Mandatory = $true)]
        [string]$FallbackPorts
    )

    $ports = @($PrimaryPort)
    foreach ($rawPort in ($FallbackPorts -split ',')) {
        $trimmedPort = $rawPort.Trim()
        if ([string]::IsNullOrWhiteSpace($trimmedPort)) {
            continue
        }

        $parsedPort = 0
        if ([int]::TryParse($trimmedPort, [ref]$parsedPort) -and $parsedPort -notin $ports) {
            $ports += $parsedPort
        }
    }

    return $ports
}

function Get-TargetModes {
    param(
        [Parameter(Mandatory = $true)]
        [string]$RequestedMode
    )

    switch ($RequestedMode) {
        'dashboard' { return @('api', 'all') }
        'cli' { return @('cli') }
        'telegram' { return @('telegram') }
        'all' { return @('all') }
        default { throw "Unsupported mode: $RequestedMode" }
    }
}

function Get-OmniProcesses {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Modes
    )

    $modePattern = ($Modes | ForEach-Object { [regex]::Escape($_) }) -join '|'
    @(Get-CimInstance Win32_Process | Where-Object {
        $_.Name -eq 'python.exe' -and $_.CommandLine -match ("main\.py\s+--mode\s+(" + $modePattern + ")(?:\s|$)")
    })
}

function Get-DashboardListeners {
    param(
        [Parameter(Mandatory = $true)]
        [int[]]$Ports
    )

    @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object {
        $_.LocalPort -in $Ports
    })
}

$targetModes = Get-TargetModes -RequestedMode $Mode
$processes = @(Get-OmniProcesses -Modes $targetModes)

if ($processes.Count -eq 0) {
    Write-Host "No OMNI $Mode process is currently running." -ForegroundColor Yellow
    exit 0
}

$processIds = @($processes | ForEach-Object { $_.ProcessId })
Stop-Process -Id $processIds -Force

$shouldCheckDashboardPort = $Mode -eq 'dashboard'
if ($shouldCheckDashboardPort) {
    $primaryPort = [int](Get-EnvSetting -Name 'API_PORT' -Default '8000')
    $fallbackPorts = Get-EnvSetting -Name 'API_FALLBACK_PORTS' -Default '8010'
    $candidatePorts = Get-CandidatePorts -PrimaryPort $primaryPort -FallbackPorts $fallbackPorts
}

$stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
while ($stopwatch.Elapsed.TotalSeconds -lt 15) {
    $remainingProcesses = @(Get-OmniProcesses -Modes $targetModes)
    if (-not $shouldCheckDashboardPort) {
        if ($remainingProcesses.Count -eq 0) {
            break
        }
    }
    else {
        $remainingListeners = @(Get-DashboardListeners -Ports $candidatePorts)
        if ($remainingProcesses.Count -eq 0 -and $remainingListeners.Count -eq 0) {
            break
        }
    }

    Start-Sleep -Milliseconds 250
}

$remainingProcesses = @(Get-OmniProcesses -Modes $targetModes)
if ($remainingProcesses.Count -eq 0) {
    Write-Host ('Stopped OMNI ' + $Mode + ' process(es): ' + ($processIds -join ', ')) -ForegroundColor Green
    exit 0
}

Write-Warning ('Some OMNI ' + $Mode + ' processes are still running. Check Task Manager or the open PowerShell windows.')
exit 1
