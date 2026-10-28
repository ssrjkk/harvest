# Публикация поля с Windows-машины (Task Scheduler)

Раз в 30 минут запускает `publish_dash.py`: свежий `state.json` (+ шифрованный
`wallets.enc.json`, если есть `WALLETS_KEY`) уезжает в ветку `gh-pages`
одним amend-коммитом. Сайт показывает снимок, витрина считает его протухшим
через 45 минут после `updated_at`.

## Разовый прогон

```powershell
$env:GITHUB_TOKEN = "ghp_..."   # запись в репозиторий (contents:write)
powershell -ExecutionPolicy Bypass -File deploy\windows\publish-dash.ps1
```

Лог — `publish-dash.log` в корне репозитория (в `.gitignore`).

## Регистрация задачи HarvestDashPublish

От имени пользователя, под которым лежит репозиторий и Python 3.12+
(нужен `python` в PATH либо поправьте путь в команде):

```powershell
$Repo = "D:\harvest"
$Action = New-ScheduledTaskAction -Execute "powershell.exe" `
  -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$Repo\deploy\windows\publish-dash.ps1`""
$Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
  -RepetitionInterval (New-TimeSpan -Minutes 30) -RepetitionDuration ([TimeSpan]::MaxValue)
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
  -DontStopOnIdleEnd -ExecutionTimeLimit (New-TimeSpan -Minutes 15)
Register-ScheduledTask -TaskName "HarvestDashPublish" `
  -Action $Action -Trigger $Trigger -Settings $Settings `
  -Description "harvest: publish state.json to gh-pages every 30 min"
```

`GITHUB_TOKEN` положите в переменные окружения этого пользователя
(Панель управления → Учётные записи → Изменить переменные среды) —
в задачу секреты не вшиваем, в репозиторий тем более.

## Проверка

- `Get-ScheduledTask HarvestDashPublish` — задача есть, `LastTaskResult` — `0`;
- `publish-dash.log` — строки `publish exit=0`;
- сайт: пилюля в шапке свежая, баннера протухания нет.

## Запуск портала (веб + Telegram-бот)

`portal-run.ps1` поднимает `python -m portal` (aiohttp на `127.0.0.1:8080` +
бот в long-polling) с кодом из git-клона и боевыми данными рядом:

```powershell
# копия скрипта кладётся в дата-каталог (рядом с farming_*.db и master.key),
# либо данные передаются явно:
powershell -ExecutionPolicy Bypass -File deploy\windows\portal-run.ps1 -DataDir D:\harvest
```

- Секреты — только User-env: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOW_IDS`;
  `PORTAL_SECRET` создаётся сам (`portal_secret.key` в дата-каталоге).
- Повторный запуск не плодит второй инстанс (guard по pid в `portal.pid`) —
  защита от `409 Conflict` двух ботов.
- Логи: `portal.log` (`PORTAL_LOG_FILE`, ротация 5×1 МБ) и `portal.err.log`
  (stderr: трейсбеки стартапа).
- Смена токена бота: обновить `TELEGRAM_BOT_TOKEN` в User-env, затем
  `Stop-Process -Id (Get-Content <данные>\portal.pid)` и запустить скрипт снова.
