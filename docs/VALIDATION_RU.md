# Проверки и границы готовности

Дата: 22.10.2026. Текущее окружение: Linux, Python 3.12.14, Node 24.19.0, npm 11.9.0, PostgreSQL 17. Проверки ниже выполнены локально в облачной машине, не в опубликованной новой задаче и не на production PBX.

| Проверка | Результат / evidence |
|---|---|
| Python/PostgreSQL suite | **45 passed**, 0 failed/0 skipped, 4.20с; `evidence/backend-tests.txt` |
| Frontend tests | **5 passed**, 2 файла, без skip; `evidence/frontend-tests.txt` |
| TypeScript strict + unused checks + Vite production build | Успешно; `evidence/frontend-build.txt` |
| Ruff + format + shell syntax | Успешно; src/tests/scripts/migrations и все scripts/*.sh |
| Alembic upgrade/current/check | head=0001, различий модели/DB не найдено; настоящая PostgreSQL, не SQLite substitute |
| Повторная установка | `scripts/install-dev.sh` выполнен повторно: frozen uv + npm ci + build + migration; `evidence/install-repeat.txt` |
| Полный startup | API + четыре раздельных процесса; `/health/ready` отвечает ready/live_calls=false; restart собственных процессов проверен |
| HTTP/worker functional smoke | Симулированный звонок completed; 100 authenticated requests, concurrency=8, errors=0, median=60.86мс, p95=96.95мс; `evidence/http-smoke.json` |
| Контракт AMI | Настоящий локальный TCP fixture: Login/Originate framing, response correlation, UserEvent и OriginateResponse с полем Response; это не живой Asterisk |
| HTTP adapters | httpx fixtures: Megafon makecall payload/callid, amoCRM marker/note, redirects/429/401/5xx/ambiguous outcomes |
| Dependency audit | npm: 0 записей; Python runtime lock export/pip-audit: известных уязвимостей не найдено; `evidence/new-*-audit.json`. Dev Python dependencies отдельно не аттестованы |
| Dev DB persistence | Данные перенесены в `.runtime/pgdata` bind mount, startup/schema и сохранение synthetic smoke campaign проверены. Для переноса использовался dump только созданных нами dev-данных; существующий чужой Postgres не менялся |
| Исходник legacy | Первый полный прогон 477: 6 failures из-за потерянных ZIP exec bits, 2 skip. После восстановления исходных mode bits: deploy-набор 9, OK/1 skip. Подробности — AUDIT_RU.md |

Один warning в Python suite: Starlette предупреждает о будущем переходе TestClient с httpx на httpx2. Он не скрыт и не отключён. CI workflow подготовлен, но на GitHub не запускался; Python 3.13 в матрице пока не проверен на этой машине.

Тесты покрывают tenant isolation и composite FK, роли/CSRF/Origin, logout/revocation/disabled-user/storage-failure, request size/rate limits, request validation без утечки паролей, идемпотентность/rollback, параллельные scheduler и channel budget, pause/consent/часовое окно непосредственно перед dispatch, дубли и порядок inbox, bounded orphan, crash/lease fencing, terminal event перед HTTP timeout, CRM retry/dead/unknown и повторяемый read-only импорт контактов.

Во время разработки новый тест окна сначала обнаружил отсутствующий импорт в самом тесте; после исправления выполнен указанный полный финальный прогон 45/45. Один промежуточный frontend-прогон столкнулся с одновременно выполнявшимся npm ci; финальные install, test и build выполнены последовательно. Ни одна проверка не была отключена для получения зелёного результата.

## Внешняя среда

Проверены имена/наличие переменных, без вывода значений. В текущей машине отсутствуют реальные ATS_AMI_SECRET, ATS_MEGAFON_API_KEY, ATS_MEGAFON_CRM_TOKEN, ATS_AMOCRM_TOKEN/ATS_AMO_ACCESS_TOKEN, tenant binding и allowlist тестовых номеров. Маски из старого UI не используются как credentials.

Безопасные запросы HEAD без авторизации: `vats326349.megapbx.ru` вернул 503, `axiomcore.amocrm.ru` — 405. Это ответы для HEAD на origin, **не проверка CRM API, ключа или тарифных возможностей**. Причина 503 не установлена; нет оснований объявлять ключ неверным или VPN отключённым. Настоящий AMI на прежней PBX не проверялся: её сетевой адрес/версия неизвестны, а старое 127.0.0.1 не обозначает доступный Asterisk в этой машине.

Не выполнены: реальные платные звонки, SIP registration/RTP/codecs/CallerID, playback на PBX, callback legs/history на данном кабинете ВАТС, настоящая amoCRM note, RLS runtime roles, полный перенос истории/записей, live soak/load на операторе и disaster recovery production. Local HTTP latency не доказывает вместимость SaaS.

## Что сохранено в облачной конфигурации

Черновик подтверждён ответом status=saved. Сохранены install_script (вызывает проверенный scripts/install-dev.sh), start_skill (рабочая директория, запуск сервисов, readiness, tests и границы live), два точных домена `vats326349.megapbx.ru`/`axiomcore.amocrm.ru` с сохранением package-manager presets, destination-scoped требования ATS_MEGAFON_API_KEY/ATS_AMOCRM_TOKEN и имена runtime-настроек/входящих секретов для стенда. Значения секретов не сохранялись.

Сохранение черновика не исполняет скрипты, не создаёт реальные секреты, не применяет настройки к работающей машине и не публикует snapshot. Пользователь должен проверить и сохранить изменения в настройках среды, заполнить секреты защищённым способом и затем опубликовать среду. Восстановление в новой задаче после публикации не проверено.

Итог: окружение разработки и simulator workflow работают; исходный новый проект пригоден для ревью и подключения закрытого live-стенда. «Production-ready SaaS» и «live-бета подтверждена» этой выдачей не заявляются. Оставшиеся gate перечислены в ROADMAP_RU.md и BETA_RUNBOOK_RU.md.

## Рабочий кабинет 0.2

Дополнительные отчёты: docs/evidence/workspace-tests.txt, workspace-frontend-tests.txt, workspace-http-smoke.json и workspace-ui-smoke.json. Проверяется реальная Alembic-миграция заполненной схемы 0001 до 0004 с сохранением контакта/кампании/попытки и без schema diff. Chromium открывает 12 разделов, создаёт контакт/кампанию, запускает simulator и открывает timeline. HTTP smoke использует API и четыре отдельных worker-процесса. Эти проверки не заменяют live UAT SIP/RTP/DTMF/CRM и не подтверждают выполнение root/updater на пользовательском сервере.
