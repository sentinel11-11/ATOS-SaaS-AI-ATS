# Контрактный harness интеграций

Локальный harness проверяет границы адаптеров без отправки запросов в МегаФон,
amoCRM или Asterisk:

```bash
cd /opt/ats-beta
runuser -u ats-beta -- .venv/bin/python scripts/provider-contract-harness.py
```

Он проверяет:

* МегаФон: путь `/crmapi/v1/makecall`, `X-API-KEY`, формат номера, `callid` и
  политику 429/5xx/401;
* amoCRM: поиск маркера `ATS#<attempt UUID>`, создание note и защиту от дублей;
* AMI: framing, запрет header injection, Login/Originate, корреляцию ActionID и
  UserEvent через настоящий локальный TCP fixture;
* запрет небезопасных URL и внешнего AMI без TLS.

Это не live-проверка. Для неё отдельно нужны реальные credentials, test
destination, разрешённый Caller ID, доступный AMI и согласованный сценарий
отката remote write.
