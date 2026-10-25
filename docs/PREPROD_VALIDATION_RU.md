# Проверка preprod-кандидата

Дата прогона: 25.10.2026. Проверки выполнены в disposable PostgreSQL, без реальных SIP/CRM доступов.

| Проверка | Результат |
|---|---|
| Backend pytest | 93 passed, 0 failed; PostgreSQL `ats_test` |
| Ruff, Python compile, shell syntax, `git diff --check` | успешно |
| Frontend Vitest | 3 файла, 8 тестов passed |
| TypeScript/Vite production build | успешно, 1599 modules transformed |
| HTTP + отдельные workers | passed: 100 запросов, concurrency 8, errors 0, median 65.78 ms, p95 121.73 ms |
| Chromium browser smoke | passed: 11 экранов, 22 действия, 0 JavaScript errors |
| Live telephony/CRM | не выполнялись; это следующий UAT-этап |

Проверенный smoke-контур создаётся только в базе с префиксом `ats_smoke`; live calls в нём запрещены. В UI smoke используется синтетический контакт и Simulator-маршрут.

## Что закрыто в этой итерации

* Секреты из настроек шифруются Fernet-ключом; установщик preprod генерирует ключ один раз и сохраняет его только в `/etc/ats-beta/ats.env`.
* Записи направляются в `/var/lib/ats-beta/recordings`, доступном отдельному пользователю `ats-beta` при `ProtectSystem=strict`.
* Worker-ы получают tenant-профили на границах job: AMI, Megafon, amoCRM, медиа, allowlist номеров и caller pool не используют настройки другого tenant.
* PostgreSQL `LISTEN/NOTIFY` получает tenant-scoped invalidation после изменений, поэтому журнал и dashboard обновляются через SSE.
* AMI originate использует проверяемые context/trunk/technology настройки; dialplan поддерживает PJSIP и chan_sip после отдельного ревью на тестовой PBX.

Изолированная установка описана в [PREPROD_SSH_RU.md](PREPROD_SSH_RU.md). На общем Asterisk live-контур остаётся выключенным: текущий legacy ATS продолжает владеть действующим транком до отдельного UAT и переключения.
