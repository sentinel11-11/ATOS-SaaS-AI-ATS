# ATS · новая основа закрытой беты

Исполняемый проект исходящего дозвона: FastAPI, PostgreSQL, React/TypeScript и независимые worker-процессы. Подготовлен по аудиту загруженного legacy ATS; код legacy не скопирован в новый runtime.

**Статус:** локальный simulator/HTTP/PostgreSQL и контрактные интеграции проверены. Подключение настоящих МегаФон ВАТС, Asterisk/Мультиком и amoCRM ещё требует live UAT. Это не готовый публичный SaaS и не полная замена всех функций старой версии.

## Документы

1. [Полный аудит](docs/AUDIT_RU.md) — архитектура, зависимости/циклы, критические находки, мёртвый код и долг с координатами исходных файлов.
2. [Целевая архитектура](docs/ARCHITECTURE_RU.md) — слои, схема, транзакции, воркеры, tenancy и auth.
3. [Roadmap из 7 фаз](docs/ROADMAP_RU.md) — файлы, проверки, риски и rollback.
4. [Примеры рабочего кода](docs/EXAMPLES_RU.md) — Pydantic/API, транзакция кампании, retry worker.
5. [Миграция и Strangler Fig](docs/MIGRATION_RU.md) — SQLite backup, mapping, ownership, cutover без обрыва текущих разговоров.
6. [Runbook беты](docs/BETA_RUNBOOK_RU.md) — запуск и подключение вашего профиля МегаФон/Мультиком/amoCRM.
7. [Результаты проверки](docs/VALIDATION_RU.md) — что действительно запускалось и что не проверено.

## Быстрый старт

Нужны Python 3.12, uv, Node 24/npm и Docker. Существующую рабочую копию используйте напрямую: отдельный Git worktree для cloud task не нужен.

```bash
cd /workspace/ATOS-SaaS-AI-ATS
bash scripts/install-dev.sh
source scripts/dev-env.sh
.venv/bin/python -m ats.cli bootstrap --name 'Бета' --login admin
bash scripts/start-dev.sh
```

Bootstrap попросит пароль скрыто и выдаст tenant UUID. На этой машине установка уже выполнена; production-аккаунт и пароль автоматически не создавались. `start-dev.sh` поднимает API и четыре worker-процесса. Самостоятельная проверка: `curl --fail http://127.0.0.1:8000/health/ready`.

Добавьте тестовый контакт с явным согласием, создайте кампанию Simulator, запустите и проверьте журнал. Для реальных интеграций используйте [профиль](deploy/beta-profile.env.example); masked passwords из старого UI не являются доступами. `ATS_AMOCRM_TOKEN` поддерживается напрямую. Секреты храните вне Git, не в React/SQLite/чатах.

## Проверки

```bash
# Создать отдельную disposable PostgreSQL DB для тестов.
docker exec atos-dev-postgres createdb -U postgres ats_test
source scripts/dev-env.sh
export ATS_TEST_DATABASE_URL="postgresql+psycopg://postgres@/ats_test?host=$(pwd)/.runtime/pgsocket"
.venv/bin/pytest
.venv/bin/ruff check src tests scripts migrations
npm --prefix frontend test
npm --prefix frontend run build
```

Если `ats_test` уже существует, `createdb` повторять не нужно. Tests требуют префикс ats_test и удаляют строки только в этой disposable DB. Не подставляйте production DATABASE_URL.

HTTP + separate-process smoke:

```bash
docker exec atos-dev-postgres createdb -U postgres ats_smoke
ATS_DATABASE_URL="postgresql+psycopg://postgres@/ats_smoke?host=$(pwd)/.runtime/pgsocket" .venv/bin/alembic upgrade head
ATS_DATABASE_URL="postgresql+psycopg://postgres@/ats_smoke?host=$(pwd)/.runtime/pgsocket" \
ATS_PUBLIC_ORIGIN=http://127.0.0.1:8017 ATS_WORKER_POLL_SECONDS=.1 \
.venv/bin/python scripts/smoke.py
```

Данные smoke синтетические, реальные звонки запрещены. Скрипт сам запускает и останавливает свои процессы; результат пишет в `artifacts/http-smoke.json`.

## Границы версии 0.1

Реализованы tenant-scoped данные и composite FK, auth/session revocation/CSRF, контакты/согласия, идемпотентные кампании, pause, лимиты каналов/дня/часового окна, durable inbox/outbox, leases/fencing, simulator, AMI transport/UserEvents, Megafon makecall/history, amoCRM notes к существующим contact IDs, read-only импорт контактов SQLite.

Пока нет голосового AI, inbound/ACD, записей/S3, автоматического повтора дозвона, импорта всей истории, billing, self-service, OAuth marketplace flow, RLS и secret vault по клиентам. Подготовка этих возможностей разбита на фазы. Сбой после возможной отправки звонка переводит попытку в unknown: система требует сверки и не создаёт дублирующий звонок.

Репозиторий проекта: https://github.com/sentinel11-11/ATOS-SaaS-AI-ATS. Публикация облачной среды и production deployment выполняются отдельно от отправки исходников в GitHub.
