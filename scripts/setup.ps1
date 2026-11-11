# Одноразовая подготовка проекта: .env, .venv, зависимости, git, первый коммит.
# Запуск (из любой папки):
#   powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\setup.ps1 -RemoteUrl https://github.com/ЛОГИН/РЕПОЗИТОРИЙ.git
# Скрипт можно запускать повторно: уже сделанное он пропускает и ничего не затирает.
param(
    [string]$RemoteUrl = "",
    [string]$GitName = "",
    [string]$GitEmail = "",
    [switch]$SkipInstall
)

$ErrorActionPreference = "Continue"  # git пишет служебные сообщения в stderr; успех проверяем по $LASTEXITCODE
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Info($m) { Write-Host "[..] $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "[OK] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "[!]  $m" -ForegroundColor Yellow }
function Fail($m) { Write-Host "[ОШИБКА] $m" -ForegroundColor Red; exit 1 }

# 1. Файл .env
if (Test-Path ".env") {
    Ok "Файл .env уже есть, не трогаю."
} elseif (Test-Path ".env.example") {
    Copy-Item ".env.example" ".env"
    Ok "Создан .env из .env.example."
} else {
    Fail "Нет файла .env.example. Запускайте скрипт из папки проекта."
}

# 2. Виртуальное окружение и библиотеки
$Py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not $SkipInstall) {
    if (-not (Test-Path $Py)) {
        Info "Создаю окружение .venv (1-2 минуты)..."
        python -m venv .venv
        if ($LASTEXITCODE -ne 0) { Fail "Не удалось создать .venv. Проверьте, что Python установлен: python --version" }
    } else {
        Ok ".venv уже есть."
    }
    Info "Устанавливаю библиотеки из requirements.txt..."
    & $Py -m pip install -q -r requirements.txt
    if ($LASTEXITCODE -ne 0) { Fail "pip не смог установить библиотеки. Скопируйте текст ошибки выше и покажите модели." }
    Ok "Библиотеки установлены."
}

# 3. Git
$null = git --version 2>&1
if ($LASTEXITCODE -ne 0) { Fail "Git не установлен. Скачайте с https://git-scm.com и запустите скрипт снова." }

if (-not (Test-Path ".git")) {
    git init -q -b main
    Ok "Создан git-репозиторий (ветка main)."
} else {
    Ok "Git-репозиторий уже есть."
}

# Имя и e-mail для коммитов (нужны git, иначе коммит не создастся)
$curName = (git config user.name) 2>$null
$curMail = (git config user.email) 2>$null
if (-not $curName) {
    if (-not $GitName) { $GitName = Read-Host "Введите ваше имя для коммитов (любое, например Ivan)" }
    git config user.name $GitName
}
if (-not $curMail) {
    if (-not $GitEmail) { $GitEmail = Read-Host "Введите ваш e-mail для коммитов (можно с GitHub)" }
    git config user.email $GitEmail
}

# Проверка: .env не должен попасть в git
$ignored = git check-ignore .env 2>$null
if (-not $ignored) { Fail "Файл .env не игнорируется git. Проверьте .gitignore: там должна быть строка .env" }

# Первый коммит
git rev-parse --verify HEAD 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    git add -A
    git commit -q -m "Initial: spec, configs, step docs"
    if ($LASTEXITCODE -ne 0) { Fail "Не удалось создать первый коммит." }
    Ok "Создан первый коммит."
} else {
    Ok "Первый коммит уже есть."
}

# 4. Удалённый репозиторий (по желанию)
$origin = git remote get-url origin 2>$null
if ($RemoteUrl) {
    if ($origin) { git remote set-url origin $RemoteUrl } else { git remote add origin $RemoteUrl }
    Info "Отправляю код на GitHub (может открыться окно входа в браузере)..."
    git push -u origin main
    if ($LASTEXITCODE -ne 0) { Warn "Push не удался. Проверьте адрес и вход в GitHub, затем повторите: scripts\setup.ps1 -RemoteUrl <адрес>" }
    else { Ok "Код отправлен на GitHub." }
} elseif ($origin) {
    Ok "Удалённый репозиторий уже подключён: $origin"
} else {
    Warn "GitHub не подключён. Это не мешает работе. Подключить позже: scripts\setup.ps1 -RemoteUrl <адрес репозитория>"
}

# 5. Что осталось сделать вручную (значения не печатаются)
Write-Host ""
$envLines = Get-Content ".env"
function Filled($name) { return [bool]($envLines | Where-Object { $_ -match "^\s*$name\s*=\s*\S+" }) }
if (Filled "OPENROUTER_API_KEY") { Ok "OPENROUTER_API_KEY заполнен." }
else { Warn "ОСТАЛОСЬ ВАМ: откройте файл .env и впишите OPENROUTER_API_KEY=ваш_ключ (см. USER_GUIDE.md, п. 0.1 и 0.3)." }
if (Filled "TELEGRAM_BOT_TOKEN") { Ok "TELEGRAM_BOT_TOKEN заполнен." }
else { Warn "TELEGRAM_BOT_TOKEN пуст. Это нормально до этапа 6 (Telegram)." }
Write-Host ""
Ok "Подготовка завершена. Дальше: scripts\start_step.ps1 -Step 1"
