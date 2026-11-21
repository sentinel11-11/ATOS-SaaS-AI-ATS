# RATS amoCRM widget v2: автоматическое определение менеджера

Этот PR устраняет первый этап ошибки connection_failed. В production отсутствует маршрут
GET /api/v1/amo/widget/v2/me; кроме того, браузерный CORS preflight не пропускает X-Auth-Token.

## Пользовательский сценарий

Менеджер открывает карточку amoCRM. Никакого входа в RATS, pop-up или кнопки
подключения нет. SDK amoCRM отправляет краткоживущий подписанный JWT через
this.$authorizedAjax, сервер проверяет его и находит
amoCRM user_id -> ATOS extension -> active operator. Администратор настраивает привязки один раз.

## Настройка администратора

Не размещать токены в виджете, GitHub или JavaScript.
В защищённом /etc/ats-beta/ats.env необходимо задать:

- ATS_AMO_WIDGET_SIGNING_SECRET — секрет именно интеграции amoCRM для проверки HS256.
  Это не REST API access token.
- ATS_AMO_WIDGET_ACCOUNT_ID — числовой ID аккаунта amoCRM.
- ATS_AMO_WIDGET_CLIENT_UUID — UUID установленной интеграции.
- ATS_AMO_WIDGET_AUDIENCE — точное значение aud JWT, производное от Redirect URI.

Ожидаемый iss берётся из сохранённого amoCRM subdomain. Он должен совпадать с
Origin браузера и подписанным iss. Подписанный JWT сверяется с секретом,
допустимым сроком, account_id, client_uuid, aud, user_id. Доверять только
переданному клиентом amo_user_id запрещено.

Существующие ATS_AMO_TENANT_ID, ATS_CRM_ENABLED=true и серверная таблица
сопоставления amoCRM user ID с extension оператора остаются источником данных.
Оператор должен иметь активную роль operator и не быть архивированным.

## Проверка

В DevTools/Network открыть GET https://axiomats.ru/api/v1/amo/widget/v2/me.
При успешной проверке получите HTTP 200, operator_name,
operator_extension, operator_status. Нет подписанного токена -> 401.
Нет настроенных параметров подписи -> 503.
Не совпадает аккаунт -> 403, нет привязки -> 409.

Ответ намеренно содержит telephony_ready=false и call_modes:
browser=false, forward=false. Это честный статус: серверная телефония
из amoCRM ещё не реализована.

Для реального звонка необходим отдельный этап:
- POST /api/v1/amo/widget/v2/calls, GET /calls/{id}, POST /calls/{id}/hangup;
- оператор-first Asterisk bridge, AMI/CEL/CDR корреляция, освобождение двух плеч;
- forward: заранее проверенный сервером номер менеджера;
- browser: WebRTC SIP через WSS с DTLS-SRTP и управлением регистрацией;
- DID ротация, транковые лимиты, согласие, CRM звонковая история и идемпотентность.

Нельзя считать это расширение готовой телефонией: API здесь только подтверждает
личность менеджера. Оно не запускает реальный звонок и не меняет Asterisk.

Документация amoCRM:
https://www.amocrm.ru/developers/content/web_sdk/mechanics
https://www.amocrm.ru/developers/content/oauth/disposable-tokens
