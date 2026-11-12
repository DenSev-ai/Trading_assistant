param(
    [string]$WorkDir = (Split-Path -Parent $PSScriptRoot),
    [string]$PythonPath = ""
)

$ErrorActionPreference = "Stop"
$WorkDir = (Resolve-Path -LiteralPath $WorkDir).Path
if (-not $PythonPath) { $PythonPath = Join-Path $WorkDir ".venv\Scripts\pythonw.exe" }
$PythonPath = (Resolve-Path -LiteralPath $PythonPath).Path
if (-not (Test-Path -LiteralPath (Join-Path $WorkDir "trading_assistant") -PathType Container)) {
    throw "В рабочей папке нет пакета trading_assistant: $WorkDir"
}
$action = New-ScheduledTaskAction -Execute $PythonPath -Argument "-m trading_assistant" -WorkingDirectory $WorkDir
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At "07:45"
$settings = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 16)
Register-ScheduledTask -TaskName "TradingAssistant" -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Get-ScheduledTask -TaskName "TradingAssistant"