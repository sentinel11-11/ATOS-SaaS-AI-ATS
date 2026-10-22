# Roadmap: от работающего legacy к SaaS

Оценки — порядок трудозатрат небольшой команды, не обещание даты релиза. Подключение операторов и согласование контрактов может изменить календарь. Фазы закрываются результатом проверки, а не только merge кода.

| Фаза | Что делаем / файлы | Проверки и gate | Риск и откат |
|---|---|---|---|
| 0. Зафиксировать поведение, 2–4 дня | Legacy `app/*`, tests, deploy; инвентарь служб, SQLite backup, baseline, secrets inventory, схема статусов. В новом проекте `docs/AUDIT_RU.md`, evidence | Реальные данные обезличены; тесты/skip отдельно; контроль restore backup; запрет согласия по умолчанию и ротация подтверждённо раскрытых ключей | Ошибочный вывод о старом поведении. Legacy остаётся владельцем звонков, никаких destructive cleanup/history rewrite |
| 1. Безопасное ядро, 1–2 недели | `config.py`, `models.py`, `security.py`, `repositories.py`, migrations; tenant/user/contact/campaign + API schema | Негативные тесты tenant isolation, composite FK, role/CSRF/logout/DB failure, idempotency и transaction rollback. До shared SaaS — RLS runtime role tests | Миграция схемы/ролей. Новая отдельная DB, старые клиенты продолжают прежний API; forward-compatible миграции |
| 2. Durable execution, 1–2 недели | `services.py`, `workers.py`, `domain.py`; outbox/inbox, allocation, leases/fencing, budgets | Два scheduler и несколько workers, crash до/после dispatch, дубли/неупорядоченные события, orphan и unknown. Ноль повторных originate одного attempt в проверенных сценариях | Неизвестный исход и зависшие слоты. Выключить новое планирование, сверить provider CDR; не очищать unknown и не запускать очередь повторно вслепую |
| 3. Закрытая live-бета, 1–2 недели + оператор | `adapters.py`, `deploy/asterisk/*`, `BETA_RUNBOOK_RU.md`, контрактные fixtures; два маршрута, amoCRM mapping и token rotation | По одному каналу и только consented test numbers. Клиент занят/не ответил/ответил; оба плеча ВАТС; duplicate webhook; CRM 429/401/timeout; AMI reconnect; двухсторонний звук; бизнес-исход совпал с CDR/CRM | Неправильная семантика провайдера/повторные звонки. Kill switch, pause, маршрутизация новых запусков обратно в legacy после drain; текущие разговоры остаются на Asterisk |
| 4. UI и feature parity, 2–4 недели по приоритету | React feature modules; старые Settings/Campaigns/Contacts/Journal → отдельные типизированные формы; operator workspace, recordings, templates, reports только после согласованного backlog | E2E: login → consent → draft → start/pause → outcome → CRM; 401/403/409/422/503; unmount/network cancellation, accessibility. Никаких имитаций «сохранено» при ошибке | Потеря полезных legacy-фич. Старые экраны и API сохраняются за routing flag; новый UI не меняет владельца старых кампаний |
| 5. Миграция и надёжная эксплуатация, 2–3 недели | Расширение `cli.py` до всех таблиц, `legacy_map`, CDC/journal, `MIGRATION_RU.md`; readiness, metrics, alerting, backup/restore/PITR, structured logs | Сверка counts/checksums/FK/consent, tenant isolation после импорта; staging rehearsal с длительностью и RPO/RTO; нагрузка API/очереди/AMI; аварийное восстановление | Несогласованный dual-write и повторный дозвон. Один writer на объект, неизменяемая origin map; dump нового журнала перед rollback, не возвращать очередь из старого snapshot |
| 6. Public SaaS и масштаб, 4–8+ недель | provider_accounts + vault, OIDC/OAuth, RLS, plans/billing/usage, media workers, object storage; очередь broker по измеренной необходимости | RLS под непривилегированной ролью, noisy-neighbor, per-tenant quotas, security review, live soak 24–72ч, cost/abuse/ретенция, disaster-recovery rehearsal. Нагрузочная цель ниже уточняется по тарифам | Неконтролируемые расходы/общий отказ. Канарейка tenants, лимиты на rollout, отдельные worker pools, feature flags; expand/contract DB без немедленного удаления старых столбцов |

## Что уже реализовано в этой выдаче

Фазы 0–2 представлены аудитом и работающим новым минимальным ядром: HTTP API, PostgreSQL/Alembic, sessions/CSRF, контакты, кампании, simulator, allocation, inbox/outbox/worker processes, AMI/Megafon/amoCRM transport и тесты. Для фазы 3 подготовлены адаптеры и чек-лист; её нельзя считать закрытой без настоящего стенда. Фазы 4–6 не объявляются выполненными.

## Предлагаемые критерии, а не измеренные обещания

* API на целевом стенде: p95 < 250 мс для списков с пагинацией при согласованной нагрузке, error rate < 1%; считать все ответы, включая ошибки/таймауты.
* Scheduler lag p95 < 2с; inbox age p95 < 5с; CRM lag отдельно от телефонии, alert на dead/unknown и рост retry.
* Ноль межтенантных чтений/связей, ноль повторных originate для одного attempt при crash injection, ни одного вызова без действующего допуска на dispatch boundary.
* Первый live gate: по 1 каналу на провайдера; далее 3, затем лимит из договора. Не превышать CPS/одновременные звонки оператора нагрузочным инструментом без согласованного окна.
* Production RPO/RTO нужно согласовать и подтвердить восстановлением. Для исходной цели разумная отправная точка RPO≤5 мин/RTO≤30 мин, но текущая бета этого не доказывает.

Сбор CDR/CEL, call setup latency, answer-seizure ratio и audio quality измеряется отдельно от HTTP. Быстрый `/health` не означает, что SIP-звонок или голосовой агент работают.
