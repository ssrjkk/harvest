#Requires -Version 5.1
<#  Подъём harvest-портала: aiohttp (веб/API на 127.0.0.1:8080) + Telegram-бот (polling).

    Кладётся рядом с боевыми данными (farming_*.db, master.key) — тогда
    дата-каталог = каталог скрипта; либо данные передаются явно:
      powershell -ExecutionPolicy Bypass -File portal-run.ps1 -DataDir D:\путь\к\данным
    Код берётся из git-клона (см. -Repo; поправьте под свою машину).

    Секреты в файл НЕ пишутся: TELEGRAM_BOT_TOKEN / TELEGRAM_ALLOW_IDS
    читаются из User-env при каждом старте. PORTAL_SECRET при первом старте
    сам создаст portal_secret.key в дата-каталоге (сесси-ключ, не секрет бота).

    Старт:  powershell -ExecutionPolicy Bypass -File portal-run.ps1
    Стоп:   Stop-Process -Id (Get-Content <данные>\portal.pid)
    Логи:   portal.log (PORTAL_LOG_FILE, ротация 5x1МБ), stderr — portal.err.log.
#>
param(
    [string]$DataDir = "",
    [string]$Repo = "D:\Dev\Temp\opencode\harvest-repo"
)
$ErrorActionPreference = "Stop"

$Dir = if ($DataDir) { $DataDir } else { Split-Path -Parent $MyInvocation.MyCommand.Path }

# повторный запуск: если прошлый процесс ещё жив - не плодить второй инстанс
# (второй polling ловит 409 Conflict и глушит оба)
$pidFile = Join-Path $Dir "portal.pid"
if (Test-Path $pidFile) {
    $old = 0
    [void][int]::TryParse([string](Get-Content $pidFile -ErrorAction SilentlyContinue), [ref]$old)
    if ($old -and (Get-Process -Id $old -ErrorAction SilentlyContinue)) {
        Write-Output "Портал уже работает: pid=$old"
        exit 0
    }
}

# секреты - только из окружения (User env), в файл не сохраняем
foreach ($n in @("TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOW_IDS")) {
    $u = [Environment]::GetEnvironmentVariable($n, "User")
    if ($u) { Set-Item -Path ("env:" + $n) -Value $u }
}
$env:PORTAL_DB          = Join-Path $Dir "farming_run200.db"
$env:PORTAL_FARM_CONFIG = Join-Path $Repo "config_vibevibe.yaml"
$env:PORTAL_LINKS       = Join-Path $Repo "portal\links.json"
$env:PORTAL_LOG_FILE    = Join-Path $Dir "portal.log"
$env:PYTHONPATH         = $Repo
$env:PYTHONIOENCODING   = "utf-8"

Set-Location $Dir
$psi = @{
    FilePath               = "C:\Users\ssrjk\AppData\Local\Programs\Python\Python312\python.exe"
    ArgumentList           = @("-m", "portal")
    WorkingDirectory       = $Dir
    WindowStyle            = "Hidden"
    RedirectStandardError  = (Join-Path $Dir "portal.err.log")    # стартовые трейсбеки
    RedirectStandardOutput = (Join-Path $Dir "portal.out.log")
    PassThru               = $true
}
$p = Start-Process @psi
Set-Content -Path $pidFile -Value $p.Id -Encoding ascii
Write-Output ("Портал запущен: pid=" + $p.Id)
