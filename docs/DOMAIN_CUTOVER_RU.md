# Переключение домена на новую версию ATS

Этот runbook переключает веб-домен `axiomats.ru` на новую изолированную версию ATS и сохраняет старую версию как быстрый откат.

На текущем сервере контуры должны выглядеть так:

* старая версия: `/opt/ats/app`, systemd `ats`, обычно `127.0.0.1:9124`;
* новая версия: `/opt/ats-beta`, systemd `ats-beta-api` и четыре `ats-beta-worker@...`, `127.0.0.1:9130`;
* Asterisk и старый SIP-транк этим переключением не перезапускаются;
* новая версия до отдельного live UAT работает с `ATS_LIVE_CALLS_ENABLED=false`.

Оплаченный домен сам по себе не выполняет deployment. Нужны DNS-запись на IP VPS, reverse proxy и HTTPS-сертификат.

## 1. DNS

В DNS-панели домена создайте или проверьте:

```text
axiomats.ru.       A       89.223.127.145
www.axiomats.ru.   CNAME   axiomats.ru.
```

Если `www` не нужен, создавайте только A-запись. На своём компьютере дождитесь, пока команда покажет IP VPS:

```powershell
Resolve-DnsName axiomats.ru -Type A
```

## 2. Подключение и резервная копия

Команды ниже выполняются в root SSH-сессии. Они не останавливают старую службу.

```bash
ssh root@89.223.127.145

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP=/var/backups/ats-cutover/$STAMP
install -d -m 0700 "$BACKUP"

systemctl is-active ats || true
systemctl is-active ats-beta-api ats-beta-worker@scheduler ats-beta-worker@telephony ats-beta-worker@inbox ats-beta-worker@crm || true
ss -ltnp | grep -E ':(9124|9130)\\b' || true

systemctl cat ats >"$BACKUP/legacy-ats.unit.txt" 2>&1 || true
tar -C /opt -czf "$BACKUP/legacy-ats-app.tgz" ats/app
tar -C /etc -czf "$BACKUP/nginx.tgz" nginx

if [ -f /var/lib/ats/ats.db ]; then
  if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 /var/lib/ats/ats.db ".backup '$BACKUP/legacy-ats.db'"
  else
    cp -a /var/lib/ats/ats.db "$BACKUP/legacy-ats.db"
  fi
fi

if [ -f /etc/ats-beta/ats.env ]; then
  install -m 0600 /etc/ats-beta/ats.env "$BACKUP/ats-beta.env"
fi
printf 'Backup: %s\\n' "$BACKUP"
```

Не отправляйте содержимое `ats.env` в чат: в нём могут быть ключи интеграций.

## 3. Обновление новой версии

Если `/opt/ats-beta` уже установлена, обновляйте только её:

```bash
cd /opt/ats-beta
test -z "$(runuser -u ats-beta -- git status --porcelain)" || {
  echo 'В /opt/ats-beta есть локальные изменения; сначала сохраните их.' >&2
  exit 1
}
bash /opt/ats-beta/deploy/ubuntu/update-preprod.sh
```

Скрипт создаёт собственный PostgreSQL backup, выполняет frozen dependency install, миграции, сборку UI и перезапускает только `ats-beta-*`.

Если новой установки ещё нет, выполняется один раз:

```bash
git clone --branch main --single-branch \
  https://github.com/sentinel11-11/ATOS-SaaS-AI-ATS.git /opt/atos-preprod-bootstrap
cd /opt/atos-preprod-bootstrap
bash deploy/ubuntu/install-preprod.sh
```

После первой установки администратор создаётся один раз:

```bash
bash /opt/ats-beta/deploy/ubuntu/manage-beta.sh -m ats.cli bootstrap \
  --name 'ATS Preprod' --login admin
```

## 4. Настройка origin для HTTPS-домена

Перед публикацией API должен знать настоящий origin. Существующий `ATS_DATABASE_URL` и ключ шифрования не меняйте.

```bash
ENV=/etc/ats-beta/ats.env
cp -a "$ENV" "$BACKUP/ats.env.before-domain"

set_env() {
  key=$1
  value=$2
  if grep -q "^${key}=" "$ENV"; then
    sed -i "s|^${key}=.*$|${key}=${value}|" "$ENV"
  else
    printf '\\n%s=%s\\n' "$key" "$value" >>"$ENV"
  fi
}

set_env ATS_PUBLIC_ORIGIN https://axiomats.ru
set_env ATS_SECURE_COOKIES true
set_env ATS_INSTANCE_NAME axiomats-preprod
set_env ATS_DEPLOYMENT preprod
set_env ATS_LIVE_CALLS_ENABLED false
set_env ATS_CRM_ENABLED false
set_env ATS_INBOUND_ENABLED false
set_env ATS_MEDIA_ENABLED false
chown root:ats-beta "$ENV"
chmod 0640 "$ENV"

systemctl restart ats-beta-api ats-beta-worker@scheduler \
  ats-beta-worker@telephony ats-beta-worker@inbox ats-beta-worker@crm
curl --fail --silent http://127.0.0.1:9130/health/ready
```

Ожидайте `status=ready`, `deployment=preprod`, `live_calls=false`.

## 5. Reverse proxy для axiomats.ru

Сначала найдите действующий блок старой версии и сохраните его имя:

```bash
nginx -T 2>/dev/null | grep -nB3 -A15 'server_name .*axiomats\\.ru'
grep -RIl 'server_name .*axiomats\\.ru' /etc/nginx/sites-enabled || true
```

Старый конфиг не удаляйте. В новом блоке укажите backend `127.0.0.1:9130`:

```bash
cat >/etc/nginx/sites-available/axiomats-new.conf <<'NGINX'
server {
    listen 80;
    listen [::]:80;
    server_name www.axiomats.ru;
    return 301 http://axiomats.ru$request_uri;
}

server {
    listen 80;
    listen [::]:80;
    server_name axiomats.ru;

    location / {
        proxy_pass http://127.0.0.1:9130;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Connection "";
        proxy_read_timeout 75s;
        proxy_send_timeout 75s;
        proxy_buffering off;
    }
}
NGINX
ln -sfn /etc/nginx/sites-available/axiomats-new.conf \
  /etc/nginx/sites-enabled/axiomats-new.conf
```

Перед reload отключите **только** старый активный symlink с `server_name axiomats.ru`, который нашли выше, сохранив его под именем вроде `*.legacy-$STAMP`. Не отключайте другие сайты. Затем:

```bash
nginx -t
systemctl reload nginx
curl --fail --silent --show-error -H 'Host: axiomats.ru' \
  http://127.0.0.1/health/ready
```

Если старый блок не отключить, Nginx может оставить домен на старом backend из-за дублирующего `server_name`.

## 6. HTTPS

После того как HTTP открывается и DNS уже указывает на VPS:

```bash
apt-get update
apt-get install -y certbot python3-certbot-nginx
certbot --nginx --redirect --agree-tos --non-interactive \
  -m YOUR_ADMIN_EMAIL -d axiomats.ru -d www.axiomats.ru
nginx -t && systemctl reload nginx
curl --fail --silent --show-error https://axiomats.ru/health/ready
```

Если DNS-запись `www` не создавалась, уберите `-d www.axiomats.ru`.
В браузере используйте канонический адрес `https://axiomats.ru`: приложение
проверяет точное значение `Origin` для изменяющих запросов.

## 7. Проверка переключения

```bash
systemctl is-active ats
systemctl is-active ats-beta-api ats-beta-worker@scheduler \
  ats-beta-worker@telephony ats-beta-worker@inbox ats-beta-worker@crm
curl --fail --silent https://axiomats.ru/health/ready
ss -ltnp | grep -E ':(9124|9130)\\b'
```

Обе службы могут быть active: старая остаётся локальным резервом, домен должен вести на `9130`. Новые звонки не включайте только из-за того, что веб-домен переключён; для live Asterisk нужны отдельные AMI user/context, SIP/RTP UAT и разрешённые тестовые номера.

## 8. Откат

Если новая версия не проходит проверку, старый процесс уже должен оставаться active. Верните старый Nginx symlink, отключив `axiomats-new.conf`, и восстановите сохранённый legacy symlink:

```bash
rm -f /etc/nginx/sites-enabled/axiomats-new.conf
mv /etc/nginx/sites-enabled/OLD_SITE.legacy-$STAMP \
   /etc/nginx/sites-enabled/OLD_SITE
nginx -t && systemctl reload nginx
systemctl is-active ats
```

Здесь `OLD_SITE` заменяется на фактическое имя, найденное в шаге 5. Новую beta-службу для разбора можно остановить отдельно:

```bash
systemctl stop ats-beta-api ats-beta-worker@scheduler \
  ats-beta-worker@telephony ats-beta-worker@inbox ats-beta-worker@crm
```

Эта команда не останавливает старую ATS, Asterisk или PostgreSQL старого контура.
