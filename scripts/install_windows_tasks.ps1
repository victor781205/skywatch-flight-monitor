[CmdletBinding()]
param(
    [Parameter()]
    [string]$InstallDir = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = 'Stop'
$resolvedRoot = (Resolve-Path -LiteralPath $InstallDir).Path
$wscript = Join-Path $env:SystemRoot 'System32\wscript.exe'
if (-not (Test-Path -LiteralPath $wscript)) {
    throw "Windows Script Host not found: $wscript"
}

$definitions = @(
    @{ Name = 'SkyWatch_AutoStart'; Script = 'start_skywatch_silent.vbs' },
    @{ Name = 'SkyWatch_Scheduler_AutoStart'; Script = 'start_scheduler_silent.vbs' },
    @{ Name = 'SkyWatch_TelegramBot_AutoStart'; Script = 'start_telegram_bot_silent.vbs' }
)

$userId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $userId
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -RestartCount 10 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -StartWhenAvailable

foreach ($definition in $definitions) {
    $script = Join-Path $resolvedRoot $definition.Script
    if (-not (Test-Path -LiteralPath $script)) {
        throw "SkyWatch launcher not found: $script"
    }
    $action = New-ScheduledTaskAction `
        -Execute $wscript `
        -Argument ('//B //Nologo "{0}"' -f $script) `
        -WorkingDirectory $resolvedRoot
    Register-ScheduledTask `
        -TaskName $definition.Name `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $principal `
        -Description 'SkyWatch local flight-monitor service auto-start' `
        -Force | Out-Null
}

Get-ScheduledTask -TaskName ($definitions.Name) |
    Select-Object TaskName, State, @{Name = 'MultipleInstances'; Expression = { $_.Settings.MultipleInstances }}
