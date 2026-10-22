# Параллельная бета на Ubuntu 24.04

Цель — разместить simulator рядом с действующей ATS. Этот сценарий не меняет функциональные границы версии 0.1 и не выполняет live-проверку операторов.

Профиль предоставленного сервера: Ubuntu 24.04 x86_64, доступно около 7.1 ГиБ RAM и 47 ГБ диска; ATS работает от пользователя ats в `/opt/ats/app` и слушает 127.0.0.1:9124; Asterisk 20.6.0, AMI 127.0.0.1:5038. Node/uv/Docker не установлены. Для этого профиля используется нативная PostgreSQL 16 из Ubuntu. Docker не требуется.

## Что создаёт установщик

* `/opt/ats-beta` — отдельная рабочая копия и `.venv`, Python 3.12.
* `/opt/ats-beta-tools` — отдельный uv и Node 24.19.0 для сборки; системный Node/Python не заменяются.
* OS user/group ats-beta, домашний каталог `/var/lib/ats-beta`.
* PostgreSQL role ats-beta и DB ats_beta, локальная peer-аутентификация через Unix socket. Пароль DB не создаётся и не требуется. PostgreSQL устанавливается как новый системный сервис; конфигурация существующих кластеров не переписывается. Это beta DB owner, не production RLS-role.
* `/etc/ats-beta/ats.env` — root:ats-beta 0640, только локальная DB/origin и отключённые live calls/CRM.
* `ats-beta-api.service` и четыре экземпляра `ats-beta-worker@.service`.
* API слушает только `127.0.0.1:9130`; внешнего доступа и nginx-конфигурации не создаётся.

Установка добавляет пакеты ОС и службу PostgreSQL. Она не вызывает restart/stop старой ats, nginx или asterisk и не читает `/var/lib/ats/ats.db`. Однако любое добавление системных пакетов на действующий сервер требует обычного окна сопровождения и существующего backup. Откат ниже останавливает бету, а не удаляет данные.

## Первая установка

В SSH root-сессии, после успешного клонирования:

```bash
git clone --branch main https://github.com/sentinel11-11/ATOS-SaaS-AI-ATS.git /opt/ats-beta
cd /opt/ats-beta
bash deploy/ubuntu/install-beta.sh
```

Если каталог уже существует, не удаляйте его и не клонируйте поверх. Для чистой копии до первой установки выполните `git -C /opt/ats-beta pull --ff-only`; если она уже принадлежит ats-beta, запускайте Git через `runuser -u ats-beta -- git -C /opt/ats-beta pull --ff-only`. Локальные изменения необходимо сохранить. Начальный установщик отказывается работать при активных beta units и сохраняет изменённые пользовательские unit/config файлы, останавливаясь с пояснением. Это не автоматический updater.

Установщик использует APT signature verification и HTTPS. Архив Node сверяется с SHASUMS256.txt официального nodejs.org. Проверки TLS и checksum не отключаются. Сборка и установка зависимостей приложения выполняются от ats-beta, через frozen uv.lock/npm ci и отдельные caches. `NEEDRESTART_MODE=l` запрещает needrestart автоматически перезапускать посторонние службы; новые пакеты могут запускать свои собственные службы.

При обрыве начальной установки её можно повторить после устранения причины, пока beta units не активны. Root-owned bootstrap marker позволяет сохранить и переиспользовать созданную ею DB без очистки данных; чужие DB/roles с такими именами не присваиваются автоматически. Изменённая/live-конфигурация не перезаписывается. При частичном запуске сначала остановите только beta units командой из раздела остановки, затем повторяйте установку.

## Администратор

```bash
bash /opt/ats-beta/deploy/ubuntu/manage-beta.sh -m ats.cli bootstrap --name 'Server beta' --login admin
```

Wrapper сбрасывает окружение и запускает Python от ats-beta с явным URL только новой БД. Пароль вводится скрыто дважды, минимум 12 символов. Сохраните напечатанный tenant_id для входа. Существующие пароли старой ATS не копируются. Не повторяйте bootstrap после успешного создания администратора без намерения создать ещё одно пространство.

## Проверка

```bash
curl --fail --silent http://127.0.0.1:9130/health/ready
systemctl is-active ats
systemctl is-active ats-beta-api ats-beta-worker@scheduler ats-beta-worker@telephony ats-beta-worker@inbox ats-beta-worker@crm
```

Ожидается ready/live_calls=false и active у старой ATS и всех пяти beta processes. Readiness подтверждает DB/schema, а не RTP или работу операторов. При ошибке: `journalctl -u ats-beta-api -n 80 --no-pager`; для scheduler — `journalctl -u ats-beta-worker@scheduler -n 80 --no-pager`. Не отправляйте конфигурации и секреты в чат.

На Windows в отдельном PowerShell:

```powershell
ssh -N -L 9130:127.0.0.1:9130 root@SERVER_IP
```

Замените SERVER_IP на адрес своего сервера. Если используете другой SSH account, замените root. Держите туннель открытым. В браузере откройте `http://127.0.0.1:9130` именно с этим host/port — Origin проверяется сервером.

Войти tenant_id/admin/заданный пароль; добавить синтетический контакт с тестовым согласием, создать Simulator campaign с одним каналом и окном 0–24, запустить и проверить completed. CRM отключена: CRM outbox может показывать dead/crm_disabled, это ожидаемо до включения интеграции.

## Остановка и дальнейшие этапы

```bash
systemctl disable --now ats-beta-api ats-beta-worker@scheduler ats-beta-worker@telephony ats-beta-worker@inbox ats-beta-worker@crm
```

Данные и файлы сохраняются, старые службы не включены в команду. PostgreSQL также сохраняется. Не удаляйте БД/роль/runtime в качестве «отката».

После успешного simulator gate: отдельный AMI user ats-beta, review/test диалплана ats-beta, разрешённый d10 CallerID, allowlist тестовых номеров, один канал и контроль общего лимита старой ATS + беты на транке mcm. Старые контексты ats-out/from-mcm/ats-in не заменяются. МегаФон требует отдельного тестового профиля либо маршрутизации webhook по владельцу звонка. Простое переключение production webhook на новую версию не является миграцией.

## Доказательства и ограничения

В среде разработки отдельно проверены 45 backend-тестов на PostgreSQL 16, сквозной simulator/HTTP workflow с пятью процессами и 100 запросами без ошибок, официальный checksum архива Node и синтаксис shell. Отчёты: `docs/evidence/postgres16-tests.txt` и `docs/evidence/postgres16-http-smoke.json`. Полный root-установщик, APT и запуск systemd на пользовательском Ubuntu-сервере **ещё не выполнялись**. В облачном контейнере нет целевых `/opt/ats-beta` и `postgresql.service`, поэтому проверка unit dependencies должна завершиться на сервере после установки prerequisites. Установщик запускает `systemd-analyze verify` перед включением units. Нельзя считать эту установку либо live-телефонию уже выполненными по наличию файлов в Git.

## Обновление кабинета 0.2

Для уже установленной и работающей беты используйте [отдельный updater и импорт](WORKSPACE_UPDATE_RU.md), а не повторный начальный installer. Новый кабинет и его ограничения описаны в [матрице переноса](FEATURE_PARITY_RU.md). На пользовательском сервере исходная бета была подтверждена выводом ready/live_calls=false и active для API/четырёх воркеров; новый updater на этом сервере ещё не выполнялся.
