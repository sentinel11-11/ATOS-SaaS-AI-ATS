# Закрытая бета: МегаФон ВАТС, Мультиком/Asterisk и amoCRM

Версия 0.3 — кандидат для изолированного preprod-тест стенда. На этой машине выполнялись simulator, контрактные HTTP-тесты, AMI socket fixture и браузерный smoke. Реальные операторы/CRM не подключались; их доступы не были предоставлены.

## Локальная среда

Нужны Python 3.12/3.13, uv, Node 24, npm, Docker для dev PostgreSQL. В production Postgres задаётся отдельным ATS_DATABASE_URL, а `dev-db.sh` не запускается.

```bash
cd /workspace/ATOS-SaaS-AI-ATS
bash scripts/install-dev.sh
source scripts/dev-env.sh
.venv/bin/python -m ats.cli bootstrap --name 'Закрытая бета' --login admin
# Пароль вводится скрыто, tenant UUID выводится после создания.
bash scripts/start-dev.sh
```

Dev PostgreSQL не слушает TCP: только Unix socket в `.runtime` под закрытым каталогом, без сохранённого пароля. Это для изолированной машины разработчика, не production auth policy. Зависимости/DB сохраняются на диске, но процессы после новой задачи нужно запускать снова. Новый Git worktree не нужен: cloud task уже изолирован.

Вход выполняется по tenant UUID и выданному паролю. Сначала добавить синтетический контакт с явным тестовым согласием; создать draft с маршрутом Simulator; запустить, проверить completed и operations. Клиентский интерфейс показывает последние 100 строк; API поддерживает offset. Бета не подразумевает импорт реальной базы абонентов для нагрузочных звонков.

## Перед live

Один явно связанный tenant на набор секретов в deployment. Без ATS_LIVE_CALLS_ENABLED=true, tenant binding, test allowlist и обязательной конфигурации start real-route возвращает 409. Нельзя пользоваться simulator как доказательством работы оператора.

| Настройка | Для чего / источник |
|---|---|
| ATS_DATABASE_URL | PostgreSQL runtime user с минимальными правами; секрет задаётся на deployment host |
| ATS_PUBLIC_ORIGIN, ATS_SECURE_COOKIES=true | Точный HTTPS origin за reverse proxy; не wildcard |
| ATS_TELEPHONY_TENANT_ID | Tenant UUID, которому принадлежат operator accounts |
| ATS_TEST_DESTINATIONS | Только согласованные собственные/тестовые +E.164, comma-separated |
| ATS_AMI_HOST/PORT/TLS/USER/SECRET/CALLER_ID | Private PBX, TLS с проверенным сертификатом; raw AMI password через защищённый runtime secret, не HTTPS proxy placeholder |
| ATS_MEGAFON_URL/USER/API_KEY/CRM_TOKEN | Точный кабинет, сотрудник для callback, исходящий API key и отдельный webhook secret |
| ATS_CRM_ENABLED, ATS_AMO_TENANT_ID/URL, ATS_AMOCRM_TOKEN | amoCRM private integration на beta account, tenant binding, HTTPS account origin; ручная rotation token |

Пароли, SIP credentials и API tokens не передавать в чат/URL/репозиторий. Переменные отсутствуют в исходном облачном окружении. Секреты для HTTPS могут подаваться через destination-scoped proxy только при поддержке этого route платформой; AMI — TCP/TLS, требует настоящего runtime binding. Конкретные allowed hostnames можно добавить в egress после получения доменов кабинетов; широкие wildcard-правила автоматически не добавлялись.

## Asterisk / Мультиком

1. Получить версию Asterisk, адрес стенда, имя PJSIP endpoint, допустимый CLI, codec/CPS/channel limits и IP/SIP registration условия оператора. Нужна выбранная поддерживаемая версия Asterisk; fixture AMI не доказывает совместимость старого релиза.
2. Проверить существующий транк вручную на тестовом аппарате. SIP registration без двустороннего RTP не является готовым звонком.
3. На выделенной тестовой PBX включить `deploy/asterisk/extensions-ats-beta.conf`, проверить существующий endpoint `mcm`, набор `8XXXXXXXXXX` и разрешённый CLI `XXXXXXXXXX`, проверить наличие prompt и диалплан через Asterisk CLI. Приложение не меняет PBX автоматически.
4. Настроить отдельного AMI user с read call/user, write originate, ACL/firewall; проверить минимальные privileges для конкретной версии. Не давать Command/config. AMI originate privilege сам по себе широк: production требует отдельного gateway/изоляции PBX.
5. TLS проверяется штатным trust store/официальным CA; для local tunnel допускается ATS_AMI_TLS=false только на loopback. Не отключать certificate verification для remote AMI.
6. Добавить один consented test destination, выбрать 1 канал и `multicom_ami`. Сопоставить attempt UUID, Local channel, PJSIP channel, UserEvent и CDR/CEL. Проверить busy, no-answer, answer/playback, hangup обеих сторон и restart worker.

Dialplan — **шаблон для ревью**, на настоящем Asterisk не загружался. Он показывает playback тестового prompt, не AI/ACD и не голосовой сценарий legacy. Семантику DIALSTATUS при hangup обоих плеч и наличие каждого terminal UserEvent нужно проверить live; missing event приводит к unknown. Входящие и записи реализованы в коде через отдельные worker/контексты, но их SIP/RTP/MixMonitor поведение всё ещё требует live UAT.

## МегаФон ВАТС

Используется контракт из legacy: POST `/crmapi/v1/makecall` с phone/user, X-API-KEY, ответ callid. Настроить HTTPS webhook `/webhooks/megafon/TENANT_UUID` с cmd=event/history и CRM token; query token не используется. Уведомления history должны быть включены.

Первый live-тест: сотрудник ответил/не ответил; клиент ответил/занят/отклонил; webhook пришёл до HTTP response; повтор history; потерянный финал; отозванный key. ACCEPTED/COMPLETED event не интерпретируются как подтверждённый успешный разговор. Итог Success/Missed/Cancel/Busy/NotAvailable/NotAllowed/NotFound берётся из history. Неизвестный status — 422, требуется уточнение контракта, а не ложный success.

API ВАТС не предоставляет медиапоток для бота. Если договор позволяет SIP-транк МегаФон, его нужно настраивать как отдельный PJSIP provider account, не подменять callback адаптером.

## amoCRM

Бета пишет **note к существующему контакту** по crm_contact_id. Контактное сопоставление задаётся явно; автоматического создания контактов/сделок, полного OAuth marketplace flow и refresh token worker нет. Используется действующий private-integration access token; 401 делает задачу dead с видимой причиной, не бесконечный retry.

Маркер note — `ATS#attempt_UUID`. До POST адаптер ищет существующую note. Непредсказуемый исход POST не повторяется автоматически. Проверить duplicate attempts, 429/Retry-After, disconnect до/после отправки, 401 и принадлежность CRM contact нужному beta account. Ни один тест на httpx mock не заменяет проверку настоящего amoCRM.

## Разбор отказов и остановка

* `/api/v1/operations` доступен admin и показывает tenant counts attempts/inbox/outbox. В API ready проверяется DB/schema, не наличие RTP и не liveness всех workers. Наблюдайте каждый worker отдельно.
* `unknown` call: сверить UUID/external_id с CDR/CEL/ВАТС. Только после получения доказательства admin вызывает `/attempts/{id}/resolve` с completed/failed и evidence. Это не повторный дозвон.
* `dead` CRM: исправить token/mapping, проверить remote marker. В бете автоматического requeue endpoint нет; через контролируемую operator DB transaction можно вернуть **подтверждённо не отправленную** задачу в pending с audit. Unknown нельзя массово reset.
* Пауза кампании останавливает новые назначения/ещё не dispatching попытки. Уже запущенные разговоры сохраняются. Для аварийной остановки новых реальных отправок — pause всех кампаний tenant и остановка telephony worker; завершение текущих каналов выполняет оператор PBX по своим правилам.
* Backup: PostgreSQL pg_dump/PITR + проверка restore, зашифрованные backup, отдельно записи. Dev DB сохраняет данные в bind mount `.runtime/pgdata`, однако это не заменяет отдельный backup/PITR и проверку восстановления. Не удаляйте runtime-каталог без dump, если начали хранить полезные данные.

Публичный SaaS запуск блокируют live UAT, RLS, runtime roles, vault/provider accounts, полная миграция/восстановление, наблюдаемость, retention, OAuth onboarding и согласованное feature parity. Конкретные требования к согласиям/хранению персональных данных нужно подтвердить для ваших сценариев и юрисдикции, а не объявлять выполненными по одному флагу consent.

## Уточнения по предоставленной конфигурации

Профиль `deploy/beta-profile.env.example` использует `https://vats326349.megapbx.ru` (без повторного `/crmapi/v1`), сотрудника admin, amoCRM `https://axiomcore.amocrm.ru` и зарезервированные для preprod значения AMI user `ats-preprod`/порт `5039`. Действующий legacy AMI user/порт не копируются. Контекст нового теста `ats-beta` специально отделён от действующего `ats-out`: не перезаписывайте production dialplan. Внутри приложения +E.164, в mcm Dial преобразование в ru8, CallerID задаётся проверенным d10 через ATS_AMI_CALLER_ID. Конкретный CLI не предоставлен — его нельзя выдумывать.

`127.0.0.1` работает только на машине с Asterisk либо при настроенном локальном защищённом туннеле; адрес старой PBX и её версия по этому значению неизвестны. REST `api.multicom.ru` не нужен выбранному SIP-маршруту. Значения `"False"`/`"True"` в старом JSON являются строками: в новом API используйте JSON booleans false/true и типизированные переменные; masked passwords не переносите как значения.
