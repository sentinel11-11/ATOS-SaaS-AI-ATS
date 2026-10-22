import type { Me } from "./types";
import { CampaignsView } from "./CampaignsView";
import { ContactsView } from "./ContactsView";
import { CallsView } from "./CallsView";
import { DashboardView, LogsView, ReportsView } from "./WorkspaceOverview";
import {
  BlacklistView,
  NumbersView,
  TemplatesView,
} from "./WorkspaceResources";
import {
  OperatorsView,
  PasswordView,
  SettingsView,
  UsersView,
} from "./WorkspaceAdmin";
import { FormEvent, useEffect, useState } from "react";

import { api, ApiError } from "./api";

function errorText(error: unknown): string {
  if (error instanceof ApiError)
    return `Не удалось выполнить действие: ${error.message}${error.requestId ? ` · ${error.requestId}` : ""}`;
  return "Нет связи с сервером. Проверьте соединение и повторите.";
}

export function App() {
  const [me, setMe] = useState<Me | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [counts, setCounts] = useState<Record<string, number>>({});

  const [tab, setTab] = useState("dashboard");
  const [live, setLive] = useState(false);
  const [notice, setNotice] = useState("");

  useEffect(() => {
    api<Me>("/auth/me")
      .then(setMe)
      .catch((e) => {
        if (!(e instanceof ApiError && e.status === 401))
          setError(errorText(e));
      })
      .finally(() => setLoading(false));
  }, []);

  async function refresh(signal?: AbortSignal) {
    const result = await api<{
      counts: Record<string, number>;
      live_calls: boolean;
    }>("/workspace/dashboard", { signal });
    if (signal?.aborted) return;
    setCounts(result.counts ?? {});
    setLive(Boolean(result.live_calls));
  }
  useEffect(() => {
    if (!me) return;
    const controller = new AbortController();
    let inFlight = false;
    async function update() {
      if (inFlight) return;
      inFlight = true;
      try {
        await refresh(controller.signal);
      } catch (e) {
        if (controller.signal.aborted) return;
        if (e instanceof ApiError && e.status === 401) setMe(null);
        else setError(errorText(e));
      } finally {
        inFlight = false;
      }
    }
    void update();
    const timer = setInterval(() => void update(), 5000);
    return () => {
      controller.abort();
      clearInterval(timer);
    };
  }, [me]);

  async function action(fn: () => Promise<void>) {
    setError("");
    setNotice("");
    setBusy(true);
    try {
      await fn();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  async function login(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = new FormData(e.currentTarget);
    await action(async () => {
      await api("/auth/login", {
        method: "POST",
        body: JSON.stringify(Object.fromEntries(form)),
      });
      setMe(await api<Me>("/auth/me"));
    });
  }
  if (loading)
    return (
      <main>
        <p role="status">Подключаемся к ATS…</p>
      </main>
    );
  if (!me)
    return (
      <main className="login">
        <p className="eyebrow">ATS / PRIVATE BETA</p>
        <h1>Рабочее пространство</h1>
        <p>Войдите с учётной записью, выданной администратором.</p>
        <form onSubmit={login}>
          <label>
            ID пространства
            <input name="tenant_id" required autoComplete="organization" />
          </label>
          <label>
            Логин
            <input name="login" required autoComplete="username" />
          </label>
          <label>
            Пароль
            <input
              name="password"
              type="password"
              required
              autoComplete="current-password"
            />
          </label>
          <button disabled={busy}>Войти</button>
        </form>
        {error && <p role="alert">{error}</p>}
      </main>
    );

  const admin = me.role === "admin";
  const workspace = {
    admin,
    busy,
    action,
    setNotice,
    role: me.role,
    userId: me.user_id,
  };
  const tabs = [
    ["dashboard", "Дашборд"],
    ["campaigns", "Кампании"],
    ["contacts", "Контакты и базы"],
    ["calls", "Журнал звонков"],
    ["numbers", "Пул номеров"],
    ["operators", "Операторы и очередь"],
    ["templates", "Сценарии"],
    ["blacklist", "Чёрный список"],
    ["reports", "Отчёты"],
    ...(admin
      ? [
          ["users", "Пользователи"],
          ["settings", "Настройки"],
          ["logs", "Логи"],
        ]
      : []),
    ["password", "Мой пароль"],
  ];
  return (
    <div className="shell">
      <aside>
        <div className="brand">
          ATS<span>Бета</span>
        </div>
        <p>Управление обзвоном</p>
        <nav>
          {tabs.map(([value, label]) => (
            <button
              key={value}
              aria-current={tab === value ? "page" : undefined}
              onClick={() => {
                setTab(value);
                setError("");
                setNotice("");
              }}
            >
              {label}
            </button>
          ))}
        </nav>
        <div className="identity">
          <b>{me.login}</b>
          <small>{me.role}</small>
          <button
            disabled={busy}
            onClick={() =>
              void action(async () => {
                await api("/auth/logout", { method: "POST" });
                setMe(null);
                setCounts({});
              })
            }
          >
            Выйти
          </button>
        </div>
      </aside>
      <main>
        <header>
          <div>
            <p className="eyebrow">РАБОЧЕЕ ПРОСТРАНСТВО</p>
            <h1>{tabs.find(([value]) => value === tab)?.[1]}</h1>
          </div>
          <span className={live ? "badge live" : "badge"}>
            {live ? "Тестовые вызовы разрешены" : "Реальные вызовы выключены"}
          </span>
        </header>
        {error && (
          <p role="alert" className="alert">
            {error}
          </p>
        )}
        {notice && <p role="status">{notice}</p>}
        <div className="stats">
          <article>
            <small>Кампании</small>
            <strong>{counts.campaigns ?? 0}</strong>
          </article>
          <article>
            <small>Доступно к обзвону</small>
            <strong>{counts.callable ?? 0}</strong>
          </article>
          <article>
            <small>Требуют сверки</small>
            <strong>{counts.unknown ?? 0}</strong>
          </article>
        </div>
        {tab === "campaigns" && (
          <CampaignsView
            admin={admin}
            busy={busy}
            action={action}
            refresh={refresh}
            setNotice={setNotice}
          />
        )}
        {tab === "contacts" && (
          <ContactsView
            admin={admin}
            busy={busy}
            action={action}
            refresh={refresh}
            setNotice={setNotice}
          />
        )}
        {tab === "calls" && <CallsView {...workspace} />}
        {tab === "dashboard" && <DashboardView />}
        {tab === "numbers" && <NumbersView {...workspace} />}
        {tab === "blacklist" && <BlacklistView {...workspace} />}
        {tab === "templates" && <TemplatesView {...workspace} />}
        {tab === "operators" && <OperatorsView {...workspace} />}
        {tab === "reports" && <ReportsView />}
        {tab === "users" && admin && <UsersView {...workspace} />}
        {tab === "settings" && admin && <SettingsView {...workspace} />}
        {tab === "logs" && admin && <LogsView {...workspace} />}
        {tab === "password" && <PasswordView {...workspace} />}
      </main>
    </div>
  );
}
