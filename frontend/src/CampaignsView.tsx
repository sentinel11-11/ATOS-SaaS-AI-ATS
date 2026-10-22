import { useState } from "react";
import { api } from "./api";
import { labels } from "./types";
import type { Actions, Contact, Campaign } from "./types";
export function CampaignsView({
  contacts,
  campaigns,
  admin,
  busy,
  action,
  refresh,
  setNotice,
}: Actions & { contacts: Contact[]; campaigns: Campaign[] }) {
  const [selected, setSelected] = useState<string[]>([]);
  const [key, setKey] = useState(crypto.randomUUID());
  return (
    <>
      <section>
        <h2>Кампании</h2>
        <p>Пауза останавливает новые вызовы. Текущие разговоры продолжаются.</p>
        {!campaigns.length ? (
          <p className="empty">
            Пока нет кампаний. Добавьте контакты и создайте первую.
          </p>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Название</th>
                <th>Маршрут</th>
                <th>Состояние</th>
                <th>Каналы</th>
                <th>Действие</th>
              </tr>
            </thead>
            <tbody>
              {campaigns.map((c) => (
                <tr key={c.id}>
                  <td>{c.name}</td>
                  <td>{labels[c.route]}</td>
                  <td>
                    <span className="badge">{labels[c.status]}</span>
                  </td>
                  <td>{c.max_channels}</td>
                  <td>
                    {admin && c.status !== "completed" && (
                      <button
                        disabled={busy}
                        onClick={() =>
                          void action(async () => {
                            await api(`/campaigns/${c.id}/state`, {
                              method: "POST",
                              body: JSON.stringify({
                                status:
                                  c.status === "running" ? "paused" : "running",
                              }),
                            });
                            await refresh();
                          })
                        }
                      >
                        {c.status === "running" ? "Пауза" : "Запустить"}
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
      {admin && (
        <section>
          <h2>Новая кампания</h2>
          <form
            onChange={() => setKey(crypto.randomUUID())}
            onSubmit={(e) => {
              e.preventDefault();
              const form = e.currentTarget;
              const data = new FormData(form);
              void action(async () => {
                await api("/campaigns", {
                  method: "POST",
                  headers: { "Idempotency-Key": key },
                  body: JSON.stringify({
                    name: data.get("name"),
                    route: data.get("route"),
                    contact_ids: selected,
                    max_channels: Number(data.get("max_channels")),
                    window_start: Number(data.get("window_start")),
                    window_end: Number(data.get("window_end")),
                  }),
                });
                form.reset();
                setSelected([]);
                setKey(crypto.randomUUID());
                setNotice("Кампания создана как черновик.");
                await refresh();
              });
            }}
          >
            <div className="fields">
              <label>
                Название
                <input name="name" required maxLength={120} />
              </label>
              <label>
                Маршрут
                <select name="route">
                  <option value="sim">Симулятор</option>
                  <option value="multicom_ami">Мультиком · Asterisk</option>
                  <option value="megafon_vats">МегаФон ВАТС · callback</option>
                </select>
              </label>
              <label>
                Каналы
                <input
                  name="max_channels"
                  type="number"
                  min="1"
                  max="3"
                  defaultValue="1"
                  required
                />
              </label>
              <label>
                С какого часа
                <input
                  name="window_start"
                  type="number"
                  min="0"
                  max="23"
                  defaultValue="9"
                  required
                />
              </label>
              <label>
                До какого часа
                <input
                  name="window_end"
                  type="number"
                  min="1"
                  max="24"
                  defaultValue="18"
                  required
                />
              </label>
            </div>
            <p>Время в часовом поясе пространства. Для беты — Europe/Moscow.</p>
            <fieldset>
              <legend>Контакты с согласием</legend>
              {contacts
                .filter((c) => c.consent && !c.blocked)
                .map((c) => (
                  <label className="check" key={c.id}>
                    <input
                      type="checkbox"
                      checked={selected.includes(c.id)}
                      onChange={(e) =>
                        setSelected((previous) =>
                          e.target.checked
                            ? [...previous, c.id]
                            : previous.filter((id) => id !== c.id),
                        )
                      }
                    />
                    {c.name} · {c.phone}
                  </label>
                ))}
            </fieldset>
            <button disabled={busy || !selected.length}>
              Создать черновик
            </button>
          </form>
        </section>
      )}
    </>
  );
}
