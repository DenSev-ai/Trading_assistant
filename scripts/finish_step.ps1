# Завершение шага: pytest + ruff, затем git add, commit "Step N" и push (если GitHub подключён).
# Запуск: powershell -ExecutionPolicy Bypass -File scripts\finish_step.ps1 -Step 1
# Используйте -NoPush, чтобы только закоммитить локально.
param(
    [Parameter(Mandatory = $true)][ValidatePattern("^[1-6][A-E]?$")][string]$Step,
    [switch]$NoPush
)

$ErrorActionPreference = "Continue"  # git пишет служебные сообщения в stderr; успех проверяем по $LASTEXITCODE
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Ok($m)   { Write-Host "[OK] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "[!]  $m" -ForegroundColor Yellow }
function Fail($m) { Write-Host "[ОШИБКА] $m" -ForegroundColor Red; exit 1 }

$Py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) { Fail "Нет .venv. Сначала выполните: scripts\setup.ps1" }
if (-not (Test-Path ".git")) { Fail "Нет git-репозитория. Сначала выполните: scripts\setup.ps1" }

Write-Host "== pytest ==" -ForegroundColor Cyan
& $Py -m pytest
if ($LASTEXITCODE -ne 0) { Fail "pytest нашёл ошибки. Коммит НЕ создан. Исправьте ошибки и запустите скрипт снова." }

Write-Host "== ruff ==" -ForegroundColor Cyan
& $Py -m ruff check .
if ($LASTEXITCODE -ne 0) { Fail "ruff нашёл замечания. Коммит НЕ создан. Исправьте их и запустите скрипт снова." }

# Защита: .env не должен попасть в коммит
$ignored = git check-ignore .env 2>$null
if (Test-Path ".env") {
    if (-not $ignored) { Fail "Файл .env не игнорируется git. Коммит остановлен, чтобы не опубликовать ключи." }
}

git add -A
$tracked = git ls-files .env
if ($tracked) { Fail "Файл .env оказался в git. Выполните: git rm --cached .env, затем запустите скрипт снова." }

$changes = git status --porcelain
if (-not $changes) {
    Ok "Изменений нет, коммит не нужен (возможно, шаг уже закоммичен)."
} else {
    git commit -q -m "Step $Step"
    if ($LASTEXITCODE -ne 0) { Fail "Не удалось создать коммит. Проверьте git config user.name и user.email (их задаёт scripts\setup.ps1)." }
    Ok "Создан коммит: Step $Step"
}

if ($NoPush) {
    Warn "Push пропущен (-NoPush)."
} else {
    $origin = git remote get-url origin 2>$null
    if (-not $origin) {
        Warn "GitHub не подключён, push пропущен. Подключить: scripts\setup.ps1 -RemoteUrl <адрес репозитория>"
    } else {
        git push -u origin HEAD
        if ($LASTEXITCODE -ne 0) { Warn "Коммит создан, но push не удался (нет сети или входа в GitHub). Повторите позже: git push" }
        else { Ok "Отправлено на GitHub." }
    }
}
Ok "Шаг $Step завершён."
