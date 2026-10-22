# Обновление установленной беты и перенос данных

Сценарий относится только к /opt/ats-beta с нативной PostgreSQL 16 на Ubuntu 24.04. Старый /opt/ats/app, служба ats, nginx и Asterisk не обновляются этим скриптом. Реальные подключения не включаются. Бета получит короткое окно обслуживания для backup/build/migrations.

## Обновить код

Сначала проверьте `runuser -u ats-beta -- git -C /opt/ats-beta status --short`: рабочая копия должна быть чистой. Сохраните локальные изменения, если они есть. Перед подтягиванием кода завершите/сверьте вызовы беты; legacy продолжает работать.

```bash
cd /opt/ats-beta
runuser -u ats-beta -- git pull --ff-only origin main
bash deploy/ubuntu/update-beta.sh
```

Скрипт останавливает scheduler, проверяет отсутствие prepared/dispatching/accepted/answered/unknown, затем останавливает только пять beta units. Если есть активные попытки, scheduler возвращается к исходному состоянию и обновление прекращается. После этого создаётся custom-format pg_dump и копия env в /var/lib/ats-beta/backups/<UTC timestamp>, выполняются frozen dependency install, UI build и alembic upgrade head. Запускаются beta units и проверяется readiness на 127.0.0.1:9130. При последующей ошибке beta units остаются остановленными, путь backup печатается. Успех скрипта обязателен перед следующим шагом; начальный install-beta.sh повторно не запускается.

Тест миграции отдельно поднимает заполненную схему 0001, обновляет до 0004, проверяет сохранность IDs/телефонов/кампании и отсутствие Alembic diff. Полный root/systemd updater в облачном контейнере не исполнялся; его shell syntax проверен. Фактическую установку подтверждают вывод скрипта и readiness на вашем сервере.

Если нужен откат, остановите только beta units. Сохраните текущие данные отдельным dump перед восстановлением предыдущего backup; восстановление БД и кода планируется вместе. После появления повторных попыток `alembic downgrade 0001` намеренно отказывается удалять их историю. Не удаляйте старую SQLite или production DB для отката беты.

После успешного обновления откройте прежний SSH-туннель, http://127.0.0.1:9130 и нажмите Ctrl+F5. Tenant ID/администратор сохраняются; bootstrap повторять не надо.

## Получить согласованный снимок SQLite

Следующие команды выполняются root на сервере и читают только старую БД. Путь снимка выбран отдельно от рабочей БД. Если файл уже существует, скрипт откажется перезаписывать его.

```bash
install -d -o ats-beta -g ats-beta -m 0700 /var/lib/ats-beta/import
python3 - <<'PY'
import sqlite3
from pathlib import Path
source = Path('/var/lib/ats/ats.db')
target = Path('/var/lib/ats-beta/import/legacy-snapshot.db')
with target.open('xb'):
    pass
target.chmod(0o600)
with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True) as src, sqlite3.connect(target) as dst:
    src.backup(dst)
print('SQLite snapshot created')
PY
chown ats-beta:ats-beta /var/lib/ats-beta/import/legacy-snapshot.db
```

Сначала dry-run. Для известных данных этого сервера tenant ID ниже — уже существующее пространство. `source-id` должен оставаться одинаковым при повторном импорте одного legacy-инстанса. `source-timezone` задаёт часовой пояс старых timestamp без offset; подтвердите его по старой установке.

```bash
bash /opt/ats-beta/deploy/ubuntu/manage-beta.sh -m ats.cli import-workspace \
  --sqlite /var/lib/ats-beta/import/legacy-snapshot.db \
  --tenant-id 785b1119-0b82-4ae8-9486-71f3da7ec894 \
  --source-id legacy-main --source-timezone Europe/Moscow
```

Вывод содержит количества, rejected IDs и коды, без паролей. Разберите rejected до применения. Импорт проверен на синтетических снимках, не на вашей рабочей БД.

Для записи в новую PostgreSQL повторите команду с `--apply`. Старые данные не изменяются. Контакты и остальные сущности импортируются отдельными транзакциями; повтор после ошибки безопасно использует mappings, не переопределяет новые пользовательские правки. Подробные ограничения переноса описаны в FEATURE_PARITY_RU.md. Голосовые файлы и секреты из SQLite автоматически не копируются.

## Отдельная подготовка голоса

После восстановления регистрации оператора и review диалплана можно подготовить только зависимости/каталоги:

```bash
bash /opt/ats-beta/deploy/asterisk/install-beta-media.sh
```

Это устанавливает espeak-ng/sox, AGI и права для локального обмена файлами. Скрипт не загружает диалплан, не перезапускает Asterisk и не включает вызовы. AGI-каталог определяется через core show settings. Отдельно требуется review/include deploy/asterisk/extensions-ats-beta.conf, настройка PJSIP внутренних endpoints операторов, ATS_MEDIA_ENABLED=true в защищённом beta env и перезапуск только beta workers. AMI account имеет read=call,user и write=originate,call; system/command/config не нужны.

Озвучка идёт через локальный TTS, ответы — DTMF. Это не ASR/LLM-разговор. В Caller ID pool используйте только разрешённые оператором номера, при первом вызове один канал и собственный телефон в server allowlist. Полный живой сценарий остаётся отдельной проверкой.
