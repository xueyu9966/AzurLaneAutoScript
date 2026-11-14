# Register the ALAS / MuMu maintenance tasks.
#
# Run once from an elevated PowerShell if normal registration is denied:
#   powershell -ExecutionPolicy Bypass -File register_tasks.ps1
#
# Remove them:
#   powershell -ExecutionPolicy Bypass -File register_tasks.ps1 -Unregister

param([switch]$Unregister)

$ErrorActionPreference = "Stop"

$root = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
$pythonw = Join-Path $root "toolkit\pythonw.exe"
$script = Join-Path $PSScriptRoot "maintenance.py"

$names = @(
    "ALAS-GG-Stop-Night",
    "ALAS-GG-Start-Night",
    "ALAS-GG-Stop-Month-End",
    "ALAS-GG-Start-Month-End"
)

if ($Unregister) {
    foreach ($name in $names) {
        Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
        Write-Host "removed $name"
    }
    exit 0
}

if (-not (Test-Path $pythonw)) { throw "pythonw not found: $pythonw" }
if (-not (Test-Path $script)) { throw "maintenance.py not found: $script" }

$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 20)

function Register-One {
    param(
        [string]$Name,
        [string]$At,
        [string]$Role,
        [string]$Description
    )
    $action = New-ScheduledTaskAction `
        -Execute $pythonw `
        -Argument "`"$script`" $Role" `
        -WorkingDirectory $root
    $trigger = New-ScheduledTaskTrigger -Daily -At $At
    Register-ScheduledTask `
        -TaskName $Name `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $principal `
        -Description $Description `
        -Force | Out-Null
    Write-Host "registered $Name @ $At ($Role)"
}

Register-One "ALAS-GG-Stop-Night" "23:50" "stop-night" `
    "ALAS: stop instances and close MuMu every night except month-end"
Register-One "ALAS-GG-Start-Night" "00:10" "start-night" `
    "ALAS: open MuMu and start instances every night except the 1st"
Register-One "ALAS-GG-Stop-Month-End" "05:50" "stop-month-end" `
    "ALAS: month-end maintenance stop (last day of month only)"
Register-One "ALAS-GG-Start-Month-End" "06:10" "start-month-end" `
    "ALAS: month-end maintenance start (last day of month only)"

Get-ScheduledTask -TaskName "ALAS-GG-*" |
    Select-Object TaskName, State |
    Format-Table -AutoSize
