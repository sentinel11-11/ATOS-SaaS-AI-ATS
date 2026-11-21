# Настройка Мультикома для первого вызова из ATOS

Рабочий маршрут — `multicom_ami`, SIP-транк Мультикома и PJSIP aliases `mcm-cli-<10 цифр>`. Подготовлены интерфейс подтверждения А-номера, выбор маршрута по умолчанию и локальные проверки полного пути API → scheduler → AMI → inbox → журнал.

## Серверный контур

В ТЗ зафиксированы VPS `89.223.127.145`, приложение `/opt/ats-beta`, Asterisk `20.6.0`, AMI `127.0.0.1:5038` и пользователь `ats-beta`. Последняя SSH-проверка из облачной среды к порту 22 получила `Connection refused`. Фактические параметры и состояние VPS ожидают проверки.

При доступном SSH выполнить инвентаризацию.

```bash
git -C /opt/ats-beta rev-parse HEAD
git -C /opt/ats-beta status --short
systemctl is-active asterisk ats-beta-api ats-beta-worker@scheduler ats-beta-worker@telephony ats-beta-worker@inbox ats-beta-worker@crm
asterisk -rx 'core show version'
asterisk -rx 'pjsip show registrations'
asterisk -rx 'pjsip show endpoints'
asterisk -rx 'dialplan show ats-beta'
asterisk -rx 'dialplan show ats-beta-answer'
asterisk -rx 'manager show command PJSIPShowEndpoint'
```

Параметры Мультикома для существующего environment-файла приведены в [deploy/multicom.env.example](../deploy/multicom.env.example). Внести tenant UUID, согласованное тестовое направление и действующий AMI secret через защищённую конфигурацию VPS. Проверить приоритет сохранённого AMI-профиля в настройках ATOS, включая `cli_endpoint_prefix`, host, port, user, TLS и secret. Tenant-профиль может переопределять соответствующие deployment-параметры.

## Проверка endpoint перед Originate

При `ATS_AMI_CLI_ENDPOINT_PREFIX=mcm-cli-` адаптер выполняет `PJSIPShowEndpoint` для выбранного alias перед каждым Originate. Ответ AMI Error приводит к `ami_endpoint_unavailable`, отправка Originate прекращается. Ошибка включает как отсутствие alias, так и отказ AMI в выполнении проверки. Проверка успешного ответа подтверждает наличие endpoint в Asterisk.

Действию `PJSIPShowEndpoint` требуется AMI write-класс `system`. В образце аккаунта указано `write = originate,call,system`, loopback ACL и `read = call,user`. Сопоставить эти права с установленной версией Asterisk и применить согласованную конфигурацию к выделенному аккаунту ATOS перед включением ротации. Основание для класса доступа проверено в регистрации действия в исходниках Asterisk 20. [Исходники Asterisk](https://github.com/asterisk/asterisk/blob/20/res/res_pjsip/pjsip_configuration.c)

## Подготовка 15 aliases

Для каждого номера подтвердить право исходящего Caller ID, формат From/PAI и привязку к SIP-аккаунту. Список разрешённых номеров хранить по одному номеру на строку. Поддерживаются 10 цифр и форматы `7`, `+7`, `8` с десятизначной национальной частью.

Из `/opt/ats-beta` сгенерировать конфигурацию для просмотра.

```bash
.venv/bin/python deploy/asterisk/build_mcm_cli_endpoints.py \
  --numbers-file /root/ats-multicom-dids.txt \
  --from-domain 95.128.224.47
```

Согласовать SIP host с действующим договором. Генератор использует существующие `transport-udp`, `mcm-aor`, `mcm-auth`. Проверить соответствие этих имён серверной конфигурации и подключение сгенерированных aliases в PJSIP. Для каждого номера команда `pjsip show endpoint mcm-cli-<10 цифр>` должна находить ожидаемый endpoint с соответствующим `from_user`.

## Импорт и подтверждение пула

1. Импортировать разрешённые номера через настройки Мультикома. Повторный импорт сохраняет существующие flags. Новые записи имеют provider `multicom_ami`, `verified=false`, `active=false`.
2. В карточке одного согласованного А-номера выбрать «Мультиком SIP / Asterisk», подтвердить право использования у оператора и установить флаг подтверждения.
3. Для этого номера разрешить исходящие вызовы и включить участие в ротации. Сохранить карточку. API проверяет подтверждение перед активацией.
4. Сверить номер карточки, имя PJSIP alias и фактический From. Остальные номера вводить после индивидуальной проверки.

## Одиночный тест из интерфейса

Создать отдельный контакт с действующим согласием на согласованное направление. В новой кампании установлены маршрут Мультикома, тип «Информатор», один канал и ноль повторных попыток. Выбрать «Без шаблона», добавить только этот контакт и проверить время кампании.

В preprod live-флаг требует tenant binding и явный список тестовых направлений. После готовности профиля, пула, aliases и согласования теста включить live-флаг через конфигурацию VPS. Запустить кампанию и проверить ответ, воспроизведение hello-world, завершение и запись в журнале. Зафиксировать AttemptID, А-номер, Б-номер, alias, SIP From/PAI, фактические CDR/CEL и события AMI. Двусторонний RTP и operator bridge проходят отдельные live UAT-сценарии.

## Локальные доказательства

Общие тестовые кампании используют маршрут Мультикома и локальный AMI TCP-стенд. HTTP smoke импортирует синтетический DID, проверяет отказ активации до подтверждения, подтверждает и активирует номер, проверяет endpoint и выполняет один Originate. Повторные и запоздалые события проходят Inbox. Тесты отказа проверяют неверный AMI secret и недоступный endpoint.

Команды локального прогона и текущий результат приведены в [CURRENT_STATE_RU.md](CURRENT_STATE_RU.md). Проверки SIP-регистрации, 15 серверных aliases, Caller ID у получателя, RTP и CDR/CEL ожидают доступного VPS.
