import ipaddress
import queue
import socket
import ssl
import threading
from uuid import uuid4

import httpx


class DeliveryUnknown(Exception):
    """The remote side may have accepted a write. Never blindly repeat it."""


class Retryable(Exception):
    def __init__(self, code="temporary_failure", retry_after=0):
        self.code, self.retry_after = code, retry_after


class Rejected(Exception):
    def __init__(self, code="provider_rejected"):
        self.code = code


def https_origin(url):
    parsed = httpx.URL(url)
    if parsed.scheme != "https" or not parsed.host or parsed.username or parsed.password:
        raise Rejected("https_origin_required")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise Rejected("origin_without_path_required")
    try:
        ip = ipaddress.ip_address(parsed.host)
    except ValueError:
        ip = None
    if ip and not ip.is_global:
        raise Rejected("private_http_destination_rejected")
    return str(parsed).rstrip("/")


def write_request(client, url, **kwargs):
    try:
        response = client.post(url, **kwargs)
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:
        raise Retryable("connect_failed") from e
    except httpx.TransportError as e:
        raise DeliveryUnknown() from e
    if response.status_code == 429:
        raw = response.headers.get("retry-after", "0")
        raise Retryable("rate_limited", int(raw) if raw.isdigit() else 60)
    if response.status_code >= 500:
        raise DeliveryUnknown()
    if not 200 <= response.status_code < 300:
        raise Rejected(f"http_{response.status_code}")
    return response


class Megafon:
    def __init__(self, config, client=None):
        self.url = https_origin(config.megafon_url)
        self.user = config.megafon_user
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(10, connect=3), follow_redirects=False
        )
        self.key = config.megafon_api_key.get_secret_value()

    def originate(self, attempt_id, phone):
        response = write_request(
            self.client,
            self.url + "/crmapi/v1/makecall",
            headers={"X-API-KEY": self.key},
            json={"phone": phone.lstrip("+"), "user": self.user},
        )
        try:
            result = str(response.json()["callid"])
        except (ValueError, TypeError, KeyError) as e:
            raise DeliveryUnknown() from e
        if not result or len(result) > 120:
            raise DeliveryUnknown()
        return result


class AmoCRM:
    """Contact notes with a stable external marker. No claim of remote exactly-once."""

    def __init__(self, config, client=None):
        self.url = https_origin(config.amo_url)
        if not httpx.URL(self.url).host.endswith(".amocrm.ru"):
            raise Rejected("amo_domain_required")
        self.token = config.amo_access_token.get_secret_value()
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(10, connect=3), follow_redirects=False
        )

    def send(self, attempt_id, crm_contact_id, status):
        headers = {"Authorization": f"Bearer {self.token}"}
        marker = f"ATS#{attempt_id}"
        path = f"/api/v4/contacts/{int(crm_contact_id)}/notes"
        # Bounded search of existing notes; do not follow externally supplied links.
        for page in range(1, 21):
            try:
                response = self.client.get(
                    self.url + path, headers=headers, params={"limit": 250, "page": page}
                )
            except httpx.TransportError as e:
                raise Retryable("crm_read_failed") from e
            if response.status_code == 204:
                break
            if response.status_code == 429 or response.status_code >= 500:
                raise Retryable("crm_read_unavailable")
            if response.status_code != 200:
                raise Rejected(f"crm_read_http_{response.status_code}")
            try:
                notes = response.json().get("_embedded", {}).get("notes", [])
                if any(marker in str(n.get("params", {}).get("text", "")) for n in notes):
                    return
            except (ValueError, TypeError, AttributeError) as e:
                raise Retryable("crm_invalid_read_response") from e
            if len(notes) < 250:
                break
        else:
            raise Rejected("crm_reconciliation_page_limit")
        write_request(
            self.client,
            self.url + path,
            headers=headers,
            json=[
                {"note_type": "common", "params": {"text": f"{marker} Результат звонка: {status}"}}
            ],
        )


class AMI:
    """One connection, one reader, correlated responses. Asterisk UserEvents carry call UUIDs."""

    def __init__(self, config, on_event):
        self.config, self.on_event = config, on_event
        self.sock = None
        self.reader = None
        self.pending = {}
        self.lock = threading.RLock()
        self.write_lock = threading.Lock()

    def connect(self):
        if self.sock is not None:
            return
        raw = socket.create_connection((self.config.ami_host, self.config.ami_port), timeout=5)
        if self.config.ami_tls:
            raw = ssl.create_default_context().wrap_socket(
                raw, server_hostname=self.config.ami_host
            )
        raw.settimeout(None)
        self.sock = raw
        self.reader = threading.Thread(target=self._read, args=(raw,), daemon=True)
        self.reader.start()
        try:
            self.action(
                "Login",
                [
                    ("Username", self.config.ami_user),
                    ("Secret", self.config.ami_secret.get_secret_value()),
                    ("Events", "on"),
                ],
            )
        except Exception:
            self.close()
            raise

    @staticmethod
    def encode(name, params, action_id):
        fields = [("Action", name), ("ActionID", action_id), *params]
        if any("\r" in str(v) or "\n" in str(v) for _, v in fields):
            raise Rejected("ami_header_injection")
        return ("\r\n".join(f"{k}: {v}" for k, v in fields) + "\r\n\r\n").encode()

    def action(self, name, params, action_id=None):
        action_id = action_id or str(uuid4())
        payload = self.encode(name, params, action_id)
        answer = queue.Queue(maxsize=1)
        with self.lock:
            self.pending[action_id] = answer
        try:
            with self.write_lock:
                if not self.sock:
                    raise Retryable("ami_disconnected")
                try:
                    self.sock.sendall(payload)
                except OSError as e:
                    raise DeliveryUnknown() from e
            try:
                response = answer.get(timeout=10)
            except queue.Empty as e:
                raise DeliveryUnknown() from e
            if response.get("Response") != "Success":
                raise Rejected("ami_action_rejected")
            return response
        finally:
            with self.lock:
                self.pending.pop(action_id, None)

    def _read(self, raw):
        try:
            with raw.makefile("rb") as stream:
                frame, size = {}, 0
                while line := stream.readline(8193):
                    size += len(line)
                    if len(line) > 8192 or size > 65536:
                        raise OSError("AMI frame limit")
                    if line in {b"\r\n", b"\n"}:
                        if "Event" in frame:
                            self.on_event(frame)
                        elif "Response" in frame:
                            with self.lock:
                                waiter = self.pending.get(frame.get("ActionID"))
                            if waiter:
                                waiter.put_nowait(frame)
                        frame, size = {}, 0
                    elif b":" in line:
                        key, value = line.decode("utf-8", "replace").split(":", 1)
                        frame[key.strip()] = value.strip()
        except (OSError, ValueError, queue.Full):
            import logging

            logging.getLogger("ats.ami").warning("ami_reader_disconnected")
        finally:
            with self.lock:
                if self.sock is raw:
                    self.sock = None
            raw.close()

    def originate(self, attempt_id, phone):
        from ats.schemas import phone_e164

        phone = phone_e164(phone)
        self.connect()
        self.action(
            "Originate",
            [
                ("Channel", f"Local/{phone}@ats-beta/n"),
                ("Context", "ats-beta-answer"),
                ("Exten", "s"),
                ("Priority", "1"),
                ("Timeout", "30000"),
                ("Async", "true"),
                ("CallerID", self.config.ami_caller_id),
                ("Variable", f"__ATS_ATTEMPT_ID={attempt_id}"),
            ],
            action_id=str(attempt_id),
        )
        return str(attempt_id)

    def close(self):
        with self.lock:
            raw, self.sock = self.sock, None
        if raw:
            try:
                raw.shutdown(socket.SHUT_RDWR)
            except OSError:
                import logging

                logging.getLogger("ats.ami").debug("ami_socket_already_closed")
            raw.close()
