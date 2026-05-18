param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('api', 'cli', 'telegram', 'all')]
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
        if (-not $match.Success) {
            continue
        }

        $value = $match.Groups[1].Value.Trim()
        if ($value.Length -ge 2) {
            $first = $value[0]
            $last = $value[$value.Length - 1]
            if (($first -eq '"' -and $last -eq '"') -or ($first -eq "'" -and $last -eq "'")) {
                $value = $value.Substring(1, $value.Length - 2)
            }
        }

        return $value
    }

    return $Default
}

function Get-PythonExecutable {
    $candidates = @(
        (Join-Path $repoRoot '.venv-1\Scripts\python.exe'),
        (Join-Path $repoRoot '.venv\Scripts\python.exe')
    )

    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) {
            return $candidate
        }
    }

    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCommand) {
        return $pythonCommand.Source
    }

    throw 'No Python interpreter found. Expected .venv-1\Scripts\python.exe, .venv\Scripts\python.exe, or a global python on PATH.'
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

function Get-OmniProcesses {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Modes
    )

    $modePattern = ($Modes | ForEach-Object { [regex]::Escape($_) }) -join '|'
    Get-CimInstance Win32_Process | Where-Object {
        $_.Name -eq 'python.exe' -and $_.CommandLine -match ("main\.py\s+--mode\s+(" + $modePattern + ")(?:\s|$)")
    }
}

function Find-DashboardPortForProcesses {
    param(
        [Parameter(Mandatory = $true)]
        [int[]]$Ports,
        [Parameter(Mandatory = $true)]
        [AllowEmptyCollection()]
        [int[]]$ProcessIds
    )

    if ($ProcessIds.Count -eq 0) {
        return $null
    }

    $listeners = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object {
        $_.LocalPort -in $Ports -and $_.OwningProcess -in $ProcessIds
    } | Sort-Object LocalPort)

    if ($listeners.Count -gt 0) {
        return $listeners[0].LocalPort
    }

    return $null
}

function Find-ResponsiveDashboardPort {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Host,
        [Parameter(Mandatory = $true)]
        [int[]]$Ports
    )

    foreach ($port in $Ports) {
        try {
            $response = Invoke-WebRequest -Uri ("http://$Host`:$port/status") -UseBasicParsing -TimeoutSec 2
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 500) {
                return $port
            }
        }
        catch {
            continue
        }
    }

    return $null
}

function Open-Dashboard {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Host,
        [Parameter(Mandatory = $true)]
        [int]$Port,
        [Parameter(Mandatory = $true)]
        [string]$Label
    )

    $dashboardUrl = "http://$Host`:$Port/dashboard"
    Write-Host "$Label at $dashboardUrl" -ForegroundColor Green
    Start-Process $dashboardUrl | Out-Null
}

function Get-ListeningCandidatePorts {
    param(
        [Parameter(Mandatory = $true)]
        [int[]]$Ports
    )

    @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object {
        $_.LocalPort -in $Ports
    } | Select-Object -ExpandProperty LocalPort -Unique | Sort-Object)
}

$apiHost = Get-EnvSetting -Name 'API_HOST' -Default '0.0.0.0'
$displayHost = switch ($apiHost) {
    '0.0.0.0' { 'localhost'; break }
    '::' { 'localhost'; break }
    '127.0.0.1' { 'localhost'; break }
    default { $apiHost }
}
$primaryPort = [int](Get-EnvSetting -Name 'API_PORT' -Default '8000')
$fallbackPorts = Get-EnvSetting -Name 'API_FALLBACK_PORTS' -Default '8010'
$candidatePorts = Get-CandidatePorts -PrimaryPort $primaryPort -FallbackPorts $fallbackPorts
$pythonExecutable = Get-PythonExecutable
$telegramToken = Get-EnvSetting -Name 'TELEGRAM_TOKEN' -Default ''

if ($Mode -eq 'telegram' -and [string]::IsNullOrWhiteSpace($telegramToken)) {
    Write-Warning 'TELEGRAM_TOKEN is not configured. Add it in .env or your environment before using start_telegram.bat.'
    exit 1
}

if ($Mode -eq 'all' -and [string]::IsNullOrWhiteSpace($telegramToken)) {
    Write-Warning 'TELEGRAM_TOKEN is not configured. All mode will still start the API/dashboard, but Telegram mode will be skipped by OMNI.'
}

$preLaunchProcesses = @(Get-OmniProcesses -Modes @($Mode))
$preLaunchProcessIds = @($preLaunchProcesses | ForEach-Object { $_.ProcessId })

if ($Mode -eq 'api') {
    $dashboardProviderProcesses = @(Get-OmniProcesses -Modes @('api', 'all'))
    $dashboardProviderIds = @($dashboardProviderProcesses | ForEach-Object { $_.ProcessId })
    $existingDashboardPort = Find-DashboardPortForProcesses -Ports $candidatePorts -ProcessIds $dashboardProviderIds
    if ($null -eq $existingDashboardPort -and $dashboardProviderIds.Count -gt 0) {
        $existingDashboardPort = Find-ResponsiveDashboardPort -Host $displayHost -Ports $candidatePorts
    }
    if ($dashboardProviderIds.Count -gt 0 -and $null -ne $existingDashboardPort) {
        Open-Dashboard -Host $displayHost -Port $existingDashboardPort -Label 'Dashboard already running'
        exit 0
    }
}
elseif ($Mode -eq 'all') {
    $existingAllPort = Find-DashboardPortForProcesses -Ports $candidatePorts -ProcessIds $preLaunchProcessIds
    if ($null -eq $existingAllPort -and $preLaunchProcessIds.Count -gt 0) {
        $existingAllPort = Find-ResponsiveDashboardPort -Host $displayHost -Ports $candidatePorts
    }
    if ($preLaunchProcessIds.Count -gt 0 -and $null -ne $existingAllPort) {
        Open-Dashboard -Host $displayHost -Port $existingAllPort -Label 'All modes already running'
        exit 0
    }
}
elseif ($Mode -eq 'telegram' -and $preLaunchProcessIds.Count -gt 0) {
    Write-Host 'Telegram mode is already running in another PowerShell window.' -ForegroundColor Green
    exit 0
}

$escapedRepoRoot = $repoRoot.Replace("'", "''")
$escapedPython = $pythonExecutable.Replace("'", "''")
$windowTitle = switch ($Mode) {
    'api' { 'OMNI Dashboard'; break }
    'cli' { 'OMNI CLI'; break }
    'telegram' { 'OMNI Telegram'; break }
    'all' { 'OMNI All Modes'; break }
}
$preLaunchListeningPorts = if ($Mode -in @('api', 'all')) {
    @(Get-ListeningCandidatePorts -Ports $candidatePorts)
}
else {
    @()
}

$commandParts = @(
    "`$host.UI.RawUI.WindowTitle = 'OMNI Agent - $windowTitle'",
    "Set-Location '$escapedRepoRoot'"
)
if ($Mode -in @('api', 'all')) {
    $commandParts += "`$env:AUTH_ENFORCE='false'"
}
$commandParts += "& '$escapedPython' '.\\main.py' --mode $Mode"
$serverCommand = $commandParts -join '; '

Write-Host "Starting OMNI in '$Mode' mode in a new PowerShell window..." -ForegroundColor Cyan
Start-Process -FilePath 'powershell.exe' -WorkingDirectory $repoRoot -ArgumentList @(
    '-NoLogo',
    '-NoExit',
    '-ExecutionPolicy',
    'Bypass',
    '-Command',
    $serverCommand
) | Out-Null

if ($Mode -in @('api', 'all')) {
    $helperTemplate = @'
$candidatePorts = @({0})
$existingPorts = @({1})
for ($i = 0; $i -lt 90; $i++) {{
    $currentPorts = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object {{ $_.LocalPort -in $candidatePorts }} | Select-Object -ExpandProperty LocalPort -Unique | Sort-Object)
    $newPorts = @($currentPorts | Where-Object {{ $_ -notin $existingPorts }})
    if ($newPorts.Count -gt 0) {{
        Start-Process ('http://{2}:' + $newPorts[0] + '/dashboard') | Out-Null
        exit 0
    }}
    if ($existingPorts.Count -eq 0 -and $currentPorts.Count -gt 0) {{
        Start-Process ('http://{2}:' + $currentPorts[0] + '/dashboard') | Out-Null
        exit 0
    }}
    Start-Sleep -Milliseconds 500
}}
'@
    $helperCommand = [string]::Format(
        $helperTemplate,
        ($candidatePorts -join ','),
        ($preLaunchListeningPorts -join ','),
        $displayHost
    )
    Start-Process -FilePath 'powershell.exe' -WindowStyle Hidden -ArgumentList @(
        '-NoLogo',
        '-NoProfile',
        '-ExecutionPolicy',
        'Bypass',
        '-Command',
        $helperCommand
    ) | Out-Null
    Write-Host "OMNI $Mode mode started in a new PowerShell window. The dashboard will open automatically when ready." -ForegroundColor Green
    exit 0
}

Write-Host "OMNI $Mode mode started in a new PowerShell window." -ForegroundColor Green
exit 0
