import { useState } from "react";
import { labels } from "./types";
import { api } from "./api";
import {
  display,
  LoadError,
  Page,
  Pager,
  Row,
  RowsTable,
  useResource,
  WorkspaceProps,
} from "./workspace";

export function DashboardView() {
  const r = useResource<{
    counts: Record<string, number>;
    tenant: Row;
    live_calls: boolean;
    active_calls: Row[];
  }>("/dashboard");
  return (
    <>
      <LoadError error={r.error} />
      <div className="stats">
        {[
          ["contacts", "Контакты"],
          ["callable", "Доступны к обзвону"],
          ["campaigns", "Кампании"],
          ["attempts", "Все попытки"],
          ["active", "Занятые каналы"],
          ["unknown", "Требуют сверки"],
        ].map(([k, title]) => (
          <article key={k}>
            <small>{title}</small>
            <strong>{r.data?.counts[k] ?? "…"}</strong>
          </article>
        ))}
      </div>
      <section>
        <h2>Активные вызовы</h2>
        <RowsTable
          rows={(r.data?.active_calls ?? []).map((c) => ({
            ...c,
            state: labels[String(c.status)],
          }))}
          columns={[
            ["contact_name", "Контакт"],
            ["phone", "Телефон"],
            ["campaign_name", "Кампания"],
            ["state", "Состояние"],
          ]}
        />
      </section>
      <section>
        <h2>Лимиты пространства</h2>
        <p>
          Каналы: {display(r.data?.tenant.max_channels)} · Попыток в сутки:{" "}
          {display(r.data?.tenant.daily_limit)} · Часовой пояс:{" "}
          {display(r.data?.tenant.timezone)}
        </p>
      </section>
    </>
  );
}
export function ReportsView() {
  const [since, setSince] = useState("");
  const [until, setUntil] = useState("");
  const params = new URLSearchParams({
    ...(since ? { since: new Date(since).toISOString() } : {}),
    ...(until ? { until: new Date(until).toISOString() } : {}),
  });
  const r = useResource<{
    total: number;
    statuses: Record<string, number>;
    completion_percent: number;
    days: { day: string; total: number; completed: number }[];
  }>(`/reports?${params}`);
  return (
    <section>
      <h2>Отчёты и результаты</h2>
      <div className="fields">
        <label>
          С даты
          <input
            type="datetime-local"
            value={since}
            onChange={(e) => setSince(e.target.value)}
          />
        </label>
        <label>
          До даты (не включая)
          <input
            type="datetime-local"
            value={until}
            onChange={(e) => setUntil(e.target.value)}
          />
        </label>
      </div>
      <LoadError error={r.error} />
      <p>
        Попыток: <b>{r.data?.total ?? 0}</b> · Завершено:{" "}
        <b>{r.data?.completion_percent ?? 0}%</b>
      </p>
      <div className="toolbar">
        {Object.entries(r.data?.statuses ?? {}).map(([s, n]) => (
          <span className="badge" key={s}>
            {labels[s] ?? s}: {n}
          </span>
        ))}
      </div>
      <h3>Динамика по дням</h3>
      <RowsTable
        rows={(r.data?.days ?? []).map((d) => ({
          id: d.day,
          ...d,
          day: new Date(d.day).toLocaleDateString("ru-RU"),
        }))}
        columns={[
          ["day", "День"],
          ["total", "Попытки"],
          ["completed", "Завершено"],
        ]}
      />
    </section>
  );
}
export function LogsView({ action, busy }: WorkspaceProps) {
  const [kind, setKind] = useState("audit");
  const [q, setQ] = useState("");
  const [offset, setOffset] = useState(0);
  const r = useResource<Page<Row>>(
    `/logs/${kind}?${new URLSearchParams({ q, offset: String(offset) })}`,
  );
  const columns: [string, string][] =
    kind === "audit"
      ? [
          ["created_at", "Время"],
          ["action", "Действие"],
          ["actor_id", "Пользователь"],
          ["entity_id", "Объект"],
        ]
      : kind === "runtime"
        ? [
            ["created_at", "Время"],
            ["level", "Уровень"],
            ["component", "Компонент"],
            ["code", "Сообщение"],
            ["request_id", "ID запроса"],
            ["method", "Метод"],
            ["path", "Маршрут"],
            ["http_status", "HTTP"],
            ["entity_id", "Объект"],
          ]
        : kind === "outbox"
          ? [
              ["available_at", "Время"],
              ["kind", "Тип"],
              ["status", "Состояние"],
              ["attempts", "Попытки"],
              ["last_error", "Ошибка"],
            ]
          : [
              ["received_at", "Время"],
              ["route", "Маршрут"],
              ["event", "Событие"],
              ["status", "Обработка"],
              ["external_id", "Внешний ID"],
            ];
  return (
    <section>
      <h2>Логи и доставка событий</h2>
      <div className="toolbar">
        <select
          aria-label="Тип журнала"
          value={kind}
          onChange={(e) => {
            setKind(e.target.value);
            setOffset(0);
          }}
        >
          <option value="audit">Аудит действий</option>
          <option value="runtime">Системные события</option>
          <option value="outbox">Фоновые задачи / CRM</option>
          <option value="inbox">Входящие события</option>
        </select>
        <input
          aria-label="Поиск в журнале"
          placeholder="Фильтр по коду / действию"
          value={q}
          onChange={(e) => {
            setQ(e.target.value);
            setOffset(0);
          }}
        />
        <button onClick={r.reload}>Обновить</button>
      </div>
      <LoadError error={r.error} />
      <RowsTable
        rows={r.data?.items ?? []}
        columns={columns}
        actions={
          kind === "outbox"
            ? (row) =>
                row.status === "dead" &&
                ["crm", "diagnostic"].includes(String(row.kind)) ? (
                  <button
                    disabled={busy}
                    onClick={() => {
                      const evidence = prompt(
                        "Основание повтора (10–80 символов)",
                      );
                      if (evidence)
                        void action(async () => {
                          await api(`/workspace/jobs/${row.id}/retry`, {
                            method: "POST",
                            body: JSON.stringify({ evidence }),
                          });
                          r.reload();
                        });
                    }}
                  >
                    Повторить
                  </button>
                ) : null
            : undefined
        }
      />
      <Pager offset={offset} total={r.data?.total ?? 0} onChange={setOffset} />
    </section>
  );
}
