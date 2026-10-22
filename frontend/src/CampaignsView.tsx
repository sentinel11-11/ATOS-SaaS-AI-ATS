import { useState } from "react";
import { api } from "./api";
import { labels } from "./types";
import type { Actions } from "./types";
import {
  Editor,
  Field,
  LoadError,
  Modal,
  Page,
  Pager,
  Row,
  RowsTable,
  useResource,
} from "./workspace";

export function CampaignsView({
  admin,
  busy,
  action,
  refresh,
  setNotice,
}: Actions) {
  const [selected, setSelected] = useState<string[]>([]);
  const [key, setKey] = useState(crypto.randomUUID());
  const [q, setQ] = useState("");
  const [offset, setOffset] = useState(0);
  const [contactQ, setContactQ] = useState("");
  const [list, setList] = useState("");
  const [editing, setEditing] = useState<Row | null>(null);
  const [detailsId, setDetailsId] = useState("");
  const campaigns = useResource<Page<Row>>(
    `/campaigns?${new URLSearchParams({ q, offset: String(offset) })}`,
  );
  const contacts = useResource<Page<Row>>(
    `/contacts?${new URLSearchParams({ q: contactQ, ...(list ? { list_id: list } : {}) })}`,
  );
  const templates = useResource<Row[]>("/templates");
  const lists = useResource<Row[]>("/lists");
  const defaults = useResource<{
    revision: number;
    providers: { default_mode: string };
  }>(admin ? "/settings" : null);
  const details = useResource<{
    campaign: Row;
    items: Row[];
    counts: Record<string, number>;
  }>(detailsId ? `/campaigns/${detailsId}` : null);
  const fields: Field[] = [
    { name: "weekdays", label: "Дни недели", type: "weekdays" },
    {
      name: "connect_on_qualify",
      label: "Переводить квалифицированных на оператора",
      type: "checkbox",
    },
    { name: "name", label: "Название", required: true },
    {
      name: "route",
      label: "Маршрут",
      type: "select",
      options: [
        ["sim", "Симулятор"],
        ["multicom_ami", "Мультиком · Asterisk"],
        ["megafon_vats", "МегаФон ВАТС · callback"],
      ],
    },
    {
      name: "mode",
      label: "Режим",
      type: "select",
      options: [
        ["message", "Сообщение"],
        ["agent", "Сценарий"],
        ["operator", "Операторская обработка"],
      ],
    },
    {
      name: "template_id",
      label: "Шаблон",
      type: "select",
      options: [
        ["", "Без шаблона"],
        ...(templates.data ?? []).map(
          (t) => [t.id, String(t.name)] as [string, string],
        ),
      ],
    },
    {
      name: "max_channels",
      label: "Каналы",
      type: "number",
      required: true,
      min: 1,
      max: 100,
    },
    {
      name: "window_start",
      label: "С какого часа",
      type: "number",
      required: true,
      min: 0,
      max: 23,
    },
    {
      name: "window_end",
      label: "До какого часа",
      type: "number",
      required: true,
      min: 1,
      max: 24,
    },
    {
      name: "retry_max",
      label: "Повторы при подтверждённом отказе",
      type: "number",
      min: 0,
      max: 5,
    },
    {
      name: "retry_delay_minutes",
      label: "Интервал повторов, минут",
      type: "number",
      min: 1,
      max: 1440,
    },
  ];
  const reload = async () => {
    campaigns.reload();
    if (detailsId) details.reload();
    await refresh();
  };
  function command(id: string, path: string) {
    void action(async () => {
      await api(`/workspace/campaigns/${id}/${path}`, { method: "POST" });
      await reload();
    });
  }
  return (
    <>
      <section>
        <div className="toolbar">
          <h2>Кампании</h2>
          <input
            aria-label="Поиск кампаний"
            placeholder="Поиск по названию"
            value={q}
            onChange={(e) => {
              setQ(e.target.value);
              setOffset(0);
            }}
          />
        </div>
        <p>
          Пауза останавливает новые вызовы. Текущие разговоры продолжаются.
          Время — Europe/Moscow; неизвестный исход требует сверки.
        </p>
        <LoadError error={campaigns.error || templates.error} />
        <RowsTable
          rows={(campaigns.data?.items ?? []).map((r) => ({
            ...r,
            state_label: labels[String(r.status)],
            route_label: labels[String(r.route)],
          }))}
          columns={[
            ["name", "Название"],
            ["route_label", "Маршрут"],
            ["state_label", "Состояние"],
            ["max_channels", "Каналы"],
          ]}
          actions={(r) => (
            <>
              <button onClick={() => setDetailsId(r.id)}>Подробнее</button>
              {admin && (
                <>
                  {r.status !== "completed" && (
                    <button
                      disabled={busy}
                      onClick={() =>
                        void action(async () => {
                          await api(`/campaigns/${r.id}/state`, {
                            method: "POST",
                            body: JSON.stringify({
                              status:
                                r.status === "running" ? "paused" : "running",
                            }),
                          });
                          await reload();
                        })
                      }
                    >
                      {r.status === "running" ? "Пауза" : "Запустить"}
                    </button>
                  )}
                  <button
                    disabled={busy || r.status === "running"}
                    onClick={() => setEditing(r)}
                  >
                    Настройки
                  </button>
                  <button
                    disabled={busy || r.status === "running"}
                    onClick={() => {
                      if (
                        confirm(
                          "Повторить только завершившиеся с ошибкой попытки? Новые вызовы могут тарифицироваться.",
                        )
                      )
                        command(r.id, "retry-failed");
                    }}
                  >
                    Повторить ошибки
                  </button>
                  <button
                    disabled={busy || r.status === "running"}
                    onClick={() => {
                      if (
                        confirm(
                          "Отменить оставшиеся контакты очереди? История сохранится.",
                        )
                      )
                        command(r.id, "clear-queue");
                    }}
                  >
                    Очистить очередь
                  </button>
                  <button
                    className="danger"
                    disabled={busy || r.status === "running"}
                    onClick={() => {
                      if (confirm("Архивировать кампанию? История сохранится."))
                        void action(async () => {
                          await api(`/workspace/campaigns/${r.id}`, {
                            method: "DELETE",
                          });
                          await reload();
                        });
                    }}
                  >
                    Архивировать
                  </button>
                </>
              )}
            </>
          )}
        />
        <Pager
          offset={offset}
          total={campaigns.data?.total ?? 0}
          onChange={setOffset}
        />
      </section>
      {admin && (
        <section>
          <h2>Новая кампания</h2>
          <div className="fields">
            <label>
              База для выбора
              <select value={list} onChange={(e) => setList(e.target.value)}>
                <option value="">Все контакты</option>
                {lists.data?.map((l) => (
                  <option key={l.id} value={l.id}>
                    {String(l.name)}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Поиск контакта
              <input
                value={contactQ}
                onChange={(e) => setContactQ(e.target.value)}
              />
            </label>
          </div>
          <fieldset>
            <legend>Контакты с согласием · выбрано {selected.length}</legend>
            <button
              type="button"
              disabled={busy}
              onClick={() =>
                void action(async () => {
                  const all: string[] = [];
                  for (let offset = 0; offset < 1000; offset += 100) {
                    const page = await api<Page<Row>>(
                      `/workspace/contacts?${new URLSearchParams({ q: contactQ, offset: String(offset), limit: "100", ...(list ? { list_id: list } : {}) })}`,
                    );
                    if (page.total > 1000) {
                      setNotice(
                        "В базе больше 1000 контактов. Разделите её или уточните поиск.",
                      );
                      return;
                    }
                    all.push(
                      ...page.items
                        .filter((c) => c.consent && !c.blocked)
                        .map((c) => c.id),
                    );
                    if (offset + 100 >= page.total) break;
                  }
                  setSelected(all);
                  setKey(crypto.randomUUID());
                })
              }
            >
              Выбрать все подходящие контакты базы / поиска
            </button>
            {contacts.data?.items
              .filter((c) => c.consent && !c.blocked)
              .map((c) => (
                <label className="check" key={c.id}>
                  <input
                    type="checkbox"
                    checked={selected.includes(c.id)}
                    onChange={(e) => {
                      setSelected((s) =>
                        e.target.checked
                          ? [...s, c.id]
                          : s.filter((id) => id !== c.id),
                      );
                      setKey(crypto.randomUUID());
                    }}
                  />
                  {String(c.name)} · {String(c.phone)}
                </label>
              ))}
          </fieldset>
          <Editor
            key={`new-${defaults.data?.revision ?? 0}`}
            fields={fields}
            initial={{
              mode: defaults.data?.providers?.default_mode ?? "message",
              connect_on_qualify: true,
              max_channels: 1,
              window_start: 0,
              window_end: 24,
              retry_max: 0,
              retry_delay_minutes: 30,
            }}
            busy={busy || !selected.length}
            submit={(values) =>
              action(async () => {
                await api("/campaigns", {
                  method: "POST",
                  headers: { "Idempotency-Key": key },
                  body: JSON.stringify({
                    ...values,
                    template_id: values.template_id || null,
                    contact_ids: selected,
                  }),
                });
                setKey(crypto.randomUUID());
                setSelected([]);
                setNotice("Кампания создана как черновик.");
                await reload();
              })
            }
          />
        </section>
      )}
      {editing && (
        <Modal title="Настройки кампании" close={() => setEditing(null)}>
          <Editor
            fields={fields}
            initial={editing}
            busy={busy}
            submit={(v) =>
              action(async () => {
                await api(`/workspace/campaigns/${editing.id}`, {
                  method: "PUT",
                  body: JSON.stringify({
                    ...v,
                    template_id: v.template_id || null,
                  }),
                });
                setEditing(null);
                await reload();
              })
            }
          />
        </Modal>
      )}
      {detailsId && (
        <Modal
          title={`Кампания: ${String(details.data?.campaign.name ?? "…")}`}
          close={() => setDetailsId("")}
        >
          <LoadError error={details.error} />
          <div className="toolbar">
            {Object.entries(details.data?.counts ?? {}).map(
              ([status, count]) => (
                <span className="badge" key={status}>
                  {labels[status] ?? status}: {count}
                </span>
              ),
            )}
          </div>
          <RowsTable
            rows={details.data?.items ?? []}
            columns={[
              ["name", "Контакт"],
              ["phone", "Телефон"],
              ["status", "Состояние"],
            ]}
          />
          {admin && selected.length > 0 && (
            <button
              disabled={busy}
              onClick={() =>
                void action(async () => {
                  await api(`/workspace/campaigns/${detailsId}/members`, {
                    method: "POST",
                    body: JSON.stringify({ contact_ids: selected }),
                  });
                  details.reload();
                  setNotice("Контакты добавлены в кампанию.");
                })
              }
            >
              Добавить выбранные контакты ({selected.length})
            </button>
          )}
        </Modal>
      )}
    </>
  );
}
