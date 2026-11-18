# Переход действующей ATOS на Мультиком — этап 1

**Production — `/opt/ats-beta`; другие каталоги (`/opt/ats`, `/opt/ats-beta-tools`) не являются целью миграции.**

Этот PR не меняет настройки VPS, не запускает звонки и не отключает рабочую интеграцию МегаФона. Структурно он подготавливает безопасный переход маршрута amoCRM; полного функционального паритета пока нет.

## Проверено на работающем VPS по диагностике 18.11.2026

- Asterisk 20.6.0; `mcm-reg` зарегистрирован; endpoint `mcm` доступен; SIP UDP 5060.
- AMI `127.0.0.1:5038`, TLS выключен; Asterisk Manager показывает только `ats`.
- Оба AMI-профиля в БД используют `user=ats-beta`, но пароли профилей не сохранены.
- Рабочий tenant берётся из `ATS_TELEPHONY_TENANT_ID` на VPS; другой tenant не должен получать его секреты.
- Контексты `ats-beta` и `ats-beta-answer` не загружены.
- Asterisk HTTP отключён, WSS и browser WebRTC endpoints не настроены.
- SIP-ключ оператора, ранее вставленный в переписку, следует заменить до production.

## Что меняет этот PR

- Виджет может не передавать маршрут, тогда его выбирает сервер на основе основных настроек `_legacy_basic.provider`.
- Разрешённые переходы: `multicom` / `ami` → `multicom_ami`; `megafon_vats` → `megafon_vats`.
- Если старый виджет принудительно отправляет маршрут другого оператора, backend отвечает `409 telephony_route_mismatch`, не совершая звонок.
- Публичный и внутренний amoCRM click-to-call используют один выбор провайдера.
- В виджете используется текущий сотрудник amoCRM, затем ответственное лицо только при отсутствии идентификатора сотрудника.
- Диагностика Multicom сопоставляется проверке **Asterisk AMI**, без обращения к API МегаФона. AMI Ping не подтверждает регистрацию `mcm` и RTP.
- Недоступная синхронизация номеров SIP не имитируется. Номера добавляются в пул Caller ID после подтверждения оператором.

## Проверки перед реальным переключением

```bash
git -C /opt/ats-beta status --short
git -C /opt/ats-beta rev-parse --short HEAD
sudo asterisk -rx "pjsip show registrations"
sudo asterisk -rx "manager show user ats-beta"
sudo asterisk -rx "dialplan show ats-beta"
sudo asterisk -rx "http show status"
```

Проверки только читают состояние.

Для полноценной миграции **отдельно необходимы**: выделенный AMI credential и безопасное хранение в рабочем tenant; диалплан прямого соединения `оператор ↔ клиент` (текущий `ats-beta-answer` playback/AGI, а scheduler блокирует operator route без media); SIP/RTP тесты с реальным звонком; WSS/TLS/DTLS-SRTP и браузерный WebRTC endpoint; тесты Busy/NoAnswer/Hangup, CDR/CEL, записи и истории amoCRM; перенос номеров. Только после этого удалять оставшиеся адаптеры, credentials, вебхуки и штатный виджет МегаФона.

AMI порт не открывать наружу; работающий `ats-out` и `from-mcm` не перезаписывать; не перезапускать Asterisk при звонках.
