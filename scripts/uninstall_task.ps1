$ErrorActionPreference = "Stop"
$task = Get-ScheduledTask -TaskName "TradingAssistant" -ErrorAction SilentlyContinue
if ($null -eq $task) {
    Write-Host "Задача TradingAssistant не найдена."
} else {
    Unregister-ScheduledTask -TaskName "TradingAssistant" -Confirm:$false
    Write-Host "Задача TradingAssistant удалена."
}