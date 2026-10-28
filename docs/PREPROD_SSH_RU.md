# Предпродакшен через SSH

Текущая версия разворачивается как отдельный preprod-контур. Legacy ATS остаётся в `/opt/ats/app` под службой `ats`; preprod живёт в `/opt/ats-beta` под пользователем `ats-beta`, в базе `ats_beta`, с API на `127.0.0.1:9130` и собственными пятью systemd units. Скрипты preprod не перезапускают старый ATS, nginx или Asterisk.

## Первая установка

Команды выполняются в root SSH-сессии. Перед установкой нужен backup действующей системы и свободное окно для добавления PostgreSQL/пакетов. Секреты в Git и чат не помещаются.

```bash
git clone --branch main --single-branch \
  https://github.com/sentinel11-11/ATOS-SaaS-AI-ATS.git /opt/atos-preprod-bootstrap
cd /opt/atos-preprod-bootstrap
bash deploy/ubuntu/install-preprod.sh
```

`install-preprod.sh` клонирует проект в `/opt/ats-beta`; каталог bootstrap после успешного запуска можно удалить только после проверки, что `/opt/ats-beta` — нужная рабочая копия. Установщик создаёт отдельные OS user, PostgreSQL role/database, venv, caches и units. `ATS_LIVE_CALLS_ENABLED`, inbound, media и CRM остаются `false`.
Также он один раз генерирует `ATS_CONNECTION_ENCRYPTION_KEY` и создаёт закрытый каталог записей `/var/lib/ats-beta/recordings`; этот ключ нужен, чтобы формы настроек могли безопасно сохранять секреты провайдеров. Ключ остаётся только в `/etc/ats-beta/ats.env` и не попадает в Git.

Создайте администратора один раз:

```bash
bash /opt/ats-beta/deploy/ubuntu/manage-beta.sh -m ats.cli bootstrap \
  --name 'ATS Preprod' --login admin
```

Проверьте, что оба контура разделены:

```bash
bash /opt/ats-beta/deploy/ubuntu/preprod-audit.sh
systemctl is-active ats
systemctl is-active ats-beta-api ats-beta-worker@scheduler \
  ats-beta-worker@telephony ats-beta-worker@inbox ats-beta-worker@crm
curl --fail --silent http://127.0.0.1:9130/health/ready
```

Для браузера с локального компьютера оставьте SSH-туннель открытым:

```bash
ssh -N -L 9130:127.0.0.1:9130 root@SERVER_IP
```

Откройте `http://127.0.0.1:9130`. В ответе ready должны быть `deployment=preprod`, `instance=ats-preprod`, `live_calls=false`.

## Обновление

На сервере выполняется только preprod updater:

```bash
# Однократно для уже установленной старой beta, где updater ещё отсутствует:
cd /opt/ats-beta
runuser -u ats-beta -- git -C /opt/ats-beta pull --ff-only origin main

bash /opt/ats-beta/deploy/ubuntu/update-preprod.sh
```

Он проверяет чистую рабочую копию, подтягивает `main` fast-forward-only, создаёт backup `ats_beta`, запускает миграции/сборку и перезапускает только `ats-beta-*`. При ошибке units беты остаются остановленными, путь backup печатается. Legacy ATS и Asterisk не трогаются.

Для отдельного снимка перед изменением используйте:

```bash
bash /opt/ats-beta/deploy/ubuntu/backup-preprod.sh
```

Снимок содержит custom-format dump `ats_beta`, закрытый `ats.env`, commit кода,
архив исходников и checksums. Записи добавляются только при явном флаге
`ATS_BACKUP_RECORDINGS=true`. Проверить snapshot без изменений можно так:

```bash
bash /opt/ats-beta/deploy/ubuntu/restore-preprod.sh \
  /var/lib/ats-beta/backups/TIMESTAMP
```

Фактическое восстановление требует явного `--apply`, останавливает только beta
units и после восстановления проверяет `/health/ready`:

```bash
bash /opt/ats-beta/deploy/ubuntu/restore-preprod.sh \
  /var/lib/ats-beta/backups/TIMESTAMP --apply
```

Каждое обновление также архивирует предыдущий commit кода в
`/var/lib/ats-beta/backups/pre-update-<UTC timestamp>`. Это позволяет сохранить
старую версию отдельно от legacy ATS и базы.

Откатить только код к такому commit можно в dry-run режиме, а затем явно
применить его. База при этом не изменяется:

```bash
bash /opt/ats-beta/deploy/ubuntu/rollback-preprod.sh \
  /var/lib/ats-beta/backups/pre-update-TIMESTAMP
bash /opt/ats-beta/deploy/ubuntu/rollback-preprod.sh \
  /var/lib/ats-beta/backups/pre-update-TIMESTAMP --apply
```

Операционный статус и аварийная остановка новых отправок:

```bash
bash /opt/ats-beta/deploy/ubuntu/status-preprod.sh
bash /opt/ats-beta/deploy/ubuntu/emergency-stop-preprod.sh
```

Аварийная команда ставит все running-кампании на паузу и останавливает только
beta telephony worker. Уже начатый канал она не завершает; его завершение
выполняется через Asterisk/PBX. После проверки worker запускается вручную:

```bash
systemctl start ats-beta-worker@telephony
```

## Граница телефонии

Preprod сейчас является безопасным simulator-контуром. Общий Asterisk не становится владельцем новых звонков: нет новой SIP-регистрации, AMI inbound и live originate. Старый ATS продолжает владеть действующим транком Multicom.

Включать реальные звонки на том же Asterisk нельзя простой сменой флага: нужен отдельный AMI user, отдельный контекст/dialplan, согласованный тестовый trunk/CLI и подтверждённый лимит каналов. До этого оставляйте `ATS_LIVE_CALLS_ENABLED=false` и не включайте `ATS_INBOUND_ENABLED`/`ATS_MEDIA_ENABLED` в `/etc/ats-beta/ats.env`. Для live preprod лучше выделенная PBX или отдельный Asterisk instance.
