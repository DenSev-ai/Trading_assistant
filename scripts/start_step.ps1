# Готовит стартовое сообщение для модели и копирует его в буфер обмена.
# Запуск: powershell -ExecutionPolicy Bypass -File scripts\start_step.ps1 -Step 1
# Затем откройте НОВУЮ задачу в Cline и нажмите Ctrl+V.
param(
    [Parameter(Mandatory = $true)][ValidatePattern("^[1-6][A-E]?$")][string]$Step
)

$ErrorActionPreference = "Continue"  # git пишет служебные сообщения в stderr; успех проверяем по $LASTEXITCODE
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$stepFile = "docs\steps\STEP_$Step.md"
if (-not (Test-Path $stepFile)) {
    Write-Host "[ОШИБКА] Нет файла $stepFile" -ForegroundColor Red
    exit 1
}

# Порядок шагов; шаги с буквой (3A, 4C ...) — подшаги, выполняются строго по порядку.
$order = @("1","2","3A","3B","3C","4A","4B","4C","4D","4E","5","6")
$idx = [array]::IndexOf($order, $Step)
if ($idx -lt 0) {
    Write-Host "[ОШИБКА] Шаг '$Step' не в списке: $($order -join ', ')" -ForegroundColor Red
    exit 1
}
if ($idx -gt 0) {
    $prevName = $order[$idx - 1]
    $prev = git log --oneline --grep "^Step $prevName$" 2>$null
    if (-not $prev) {
        Write-Host "[!] Не найден коммит 'Step $prevName'. Возможно, предыдущий шаг не завершён. Если уверены, что завершён, продолжайте." -ForegroundColor Yellow
    }
}

$msg = @"
Прочитай $Root\docs\SPEC.md, $Root\docs\CONTRACTS.md и $Root\docs\steps\STEP_$Step.md. Выполни ТОЛЬКО шаг $Step по этим документам. Другие файлы в docs не читай и не меняй. Работай в папке $Root. Файлы в tests\acceptance не редактируй и не удаляй: это эталонные тесты, они задают правила; если код не проходит их — исправляй код.
Проверка: запусти pytest и ruff check . (через .venv\Scripts\python.exe -m ...) и исправь ошибки.
Когда обе проверки проходят, сам выполни команду, она ещё раз проверит код и сделает git-коммит с отправкой:
powershell -ExecutionPolicy Bypass -File scripts\finish_step.ps1 -Step $Step
Если команда вернула ошибку, исправь причину и запусти её снова. Файл .env не читай, не выводи и не коммить. В конце напиши «Шаг $Step готов.» и одной-двумя фразами, что именно мне проверить вручную.
"@

Set-Clipboard -Value $msg
Write-Host $msg
Write-Host ""
Write-Host "[OK] Сообщение скопировано в буфер обмена. Откройте новую задачу в Cline и нажмите Ctrl+V." -ForegroundColor Green
