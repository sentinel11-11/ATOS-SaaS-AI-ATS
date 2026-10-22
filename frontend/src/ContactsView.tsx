import { useState } from "react";
import { api } from "./api";
import type { Actions, Contact } from "./types";
export function ContactsView({
  contacts,
  admin,
  busy,
  action,
  refresh,
  setNotice,
}: Actions & { contacts: Contact[] }) {
  const [consent, setConsent] = useState(false);
  return (
    <>
      <section>
        <h2>Контакты · последние 100</h2>
        <table>
          <thead>
            <tr>
              <th>Имя</th>
              <th>Телефон</th>
              <th>Допуск</th>
              <th>Действие</th>
            </tr>
          </thead>
          <tbody>
            {contacts.map((c) => (
              <tr key={c.id}>
                <td>{c.name}</td>
                <td>{c.phone}</td>
                <td>
                  {c.blocked
                    ? "Заблокирован"
                    : c.consent
                      ? "Согласие есть"
                      : "Нет согласия"}
                </td>
                <td>
                  {admin && !c.blocked && (
                    <button
                      disabled={busy}
                      onClick={() =>
                        void action(async () => {
                          await api(`/contacts/${c.id}`, {
                            method: "PATCH",
                            body: JSON.stringify({
                              consent: false,
                              consent_source: "Отзыв в интерфейсе",
                              blocked: true,
                            }),
                          });
                          await refresh();
                        })
                      }
                    >
                      Запретить звонки
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
      {admin && (
        <section>
          <h2>Добавить контакт</h2>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              const form = e.currentTarget;
              const data = new FormData(form);
              void action(async () => {
                await api("/contacts", {
                  method: "POST",
                  body: JSON.stringify({
                    name: data.get("name"),
                    phone: data.get("phone"),
                    consent: data.get("consent") === "on",
                    consent_source: data.get("consent_source"),
                    crm_contact_id: data.get("crm_contact_id")
                      ? Number(data.get("crm_contact_id"))
                      : null,
                  }),
                });
                form.reset();
                setConsent(false);
                setNotice("Контакт добавлен.");
                await refresh();
              });
            }}
          >
            <div className="fields">
              <label>
                Имя
                <input name="name" required maxLength={120} />
              </label>
              <label>
                Телефон
                <input name="phone" type="tel" placeholder="+79991234567" required />
                <small>Международный формат с +, например +79991234567.</small>
              </label>
              <label>
                Источник согласия
                <input name="consent_source" required={consent} maxLength={256} />
              </label>
              <label>
                ID контакта amoCRM
                <input name="crm_contact_id" type="number" min="1" />
              </label>
            </div>
            <label className="check">
              <input
                name="consent"
                type="checkbox"
                checked={consent}
                onChange={(e) => setConsent(e.target.checked)}
              />
              Есть подтверждённое согласие на звонок
            </label>
            <button disabled={busy}>Добавить</button>
          </form>
        </section>
      )}
    </>
  );
}
