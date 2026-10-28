#Requires -Version 5.1
<#
  Публикация поля harvest в gh-pages (разовый прогон).

  Расписание — Windows Task Scheduler, задача HarvestDashPublish, каждые
  30 минут (регистрация — в README.md рядом). Руками: powershell -ExecutionPolicy Bypass -File deploy\windows\publish-dash.ps1

  Требуется GITHUB_TOKEN в окружении (ghp_* с записью в репозиторий):
  это страховочный ключ — сам push идёт через обычный git-доступ к origin
  (credential helper / gh auth / User-переменная GH_TOKEN). Если ключа нет,
  задача падает сразу с понятной ошибкой, а не с cryptic-128 от git.
  Лог дописывается в publish-dash.log рядом с publish_dash.py (в .gitignore).
#>
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$Stamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
"[$Stamp] publish start" | Out-File (Join-Path $Root "publish-dash.log") -Append -Encoding utf8

try {
    if (-not $env:GITHUB_TOKEN) { throw "GITHUB_TOKEN не задан в окружении задачи" }
    & python (Join-Path $Root "publish_dash.py")
    $Code = $LASTEXITCODE
    "[$Stamp] publish exit=$Code" | Out-File (Join-Path $Root "publish-dash.log") -Append -Encoding utf8
    exit $Code
}
catch {
    "[$Stamp] publish FAILED: $($_.Exception.Message)" | Out-File (Join-Path $Root "publish-dash.log") -Append -Encoding utf8
    throw
}
