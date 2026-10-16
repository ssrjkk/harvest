# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Mini App (этап 3 ТЗ): `portal/miniapp.py` (`/app/`, `/api/miniapp/*`,
  initData в заголовке, uniform-401, one-time confirm-токены, ETag/304,
  экспорт тем же кодом бота в чат), клиент без фреймворков
  (`portal/static/miniapp/`), `PORTAL_MINIAPP_URL`/`PORTAL_MINIAPP_PUBLIC`,
  Caddy только на Mini App-пути, auth отклоняет будущие auth_date
- Бот v1 (этап 2 ТЗ): `portal/bot_render.py` (единый рендер, RU/EN),
  `portal/wallet_cells.py` (общие клетки iwoer), `portal/bot_settings.py`
  (режим/язык), домашнее Поле на editMessageText, навигация `v1:*` ≤ 64 байт,
  мастер 3 шага с TTL, Live 10 мин, шифрованный экспорт .enc, ключи с
  подтверждением, setMyCommands RU, трекер 409, редакция токена в логах

## [2.3.0] - 2026-10-16

### Added
- Telegram-экспорт с подтверждением (export:csv/json → export:yes:*), protect_content + автоудаление 60с
- Кнопки [ДОМОЙ] (menu:home) на всех экранах бота: stats/status/doctor/history/wallets/networks/links/ref
- Антифлуд тяжёлых операций бота (30с) и живой мониторинг с алертом остановки
- Защитный контур портала (shield): risk/limits/idem/sanitize/breaker/guard
- GitHub Pages site (docs/): live field with wallet grid, beginner/pro demo zone,
  MIT section, RU/EN, Paper/Ink/Moss themes
- Telegram bot ops-style texts + /cmd, /cfg, /check, /ref, FSM wizards
- Opt-in load tests (HARVEST_LOAD=1) and site verification tooling
- Design tokens (design/tokens.json) with WCAG AA contrast table

### Fixed
- Restored network configs, harvest.spec, build.bat, root publish_dash.py
- Fail-closed seed writes without master key
- RISK_MEDIUM env-дефолт 20.0 → 25.0 (синхрон с кодом); choke-лимиты отделены от login-лимитов

### Security
- Backup files get owner-only permissions before replace
- No wallet addresses published to gh-pages (ordinals only)

## [1.0.0] - 2026-10-09

### Added
- Initial release
- Analytics dashboard
- Metrics visualization
- Repository statistics
