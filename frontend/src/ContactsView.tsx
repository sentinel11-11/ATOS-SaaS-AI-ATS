import { useState } from "react";
import { api } from "./api";
import type { Actions } from "./types";
import {
  downloadCsv,
  Editor,
  LoadError,
  Modal,
  Page,
  Pager,
  Row,
  RowsTable,
  useResource,
} from "./workspace";

const editFields = [
  { name: "name", label: "Имя", required: true },
  { name: "phone", label: "Телефон", required: true },
  { name: "group", label: "Группа" },
  { name: "email", label: "Email" },
  { name: "notes", label: "Заметки", type: "textarea" as const },
  { name: "consent", label: "Есть согласие", type: "checkbox" as const },
  { name: "consent_source", label: "Источник согласия" },
  { name: "blocked", label: "Звонки запрещены", type: "checkbox" as const },
  {
    name: "crm_contact_id",
    label: "ID контакта amoCRM",
    type: "number" as const,
    min: 1,
  },
];
export function ContactsView({
  admin,
  busy,
  action,
  refresh,
  setNotice,
}: Actions) {
  const [consent, setConsent] = useState(false);
  const [q, setQ] = useState("");
  const [offset, setOffset] = useState(0);
  const [list, setList] = useState("");
  const [editing, setEditing] = useState<Row | null>(null);
  const [history, setHistory] = useState<{ contact: Row; calls: Row[] } | null>(
    null,
  );
  const [importText, setImportText] = useState("");
  const [importFile, setImportFile] = useState<File | null>(null);
  const [importConsent, setImportConsent] = useState(false);
  const [source, setSource] = useState("");
  const contacts = useResource<Page<Row>>(
    `/contacts?${new URLSearchParams({ q, offset: String(offset), ...(list ? { list_id: list } : {}) })}`,
  );
  const lists = useResource<Row[]>("/lists");
  const reload = async () => {
    contacts.reload();
    lists.reload();
    await refresh();
  };
  return (
    <>
      <section>
        <div className="toolbar">
          <h2>Контакты</h2>
          <input
            aria-label="Поиск контактов"
            placeholder="Имя, телефон, email"
            value={q}
            onChange={(e) => {
              setQ(e.target.value);
              setOffset(0);
            }}
          />
          <select
            aria-label="База контактов"
            value={list}
            onChange={(e) => {
              setList(e.target.value);
              setOffset(0);
            }}
          >
            <option value="">Все базы</option>
            {lists.data?.map((l) => (
              <option key={l.id} value={l.id}>
                {String(l.name)} ({String(l.count)})
              </option>
            ))}
          </select>
          {admin && (
            <button onClick={() => void action(() => downloadCsv("contacts"))}>
              Экспорт CSV
            </button>
          )}
        </div>
        <LoadError error={contacts.error || lists.error} />
        <RowsTable
          rows={contacts.data?.items ?? []}
          columns={[
            ["name", "Имя"],
            ["phone", "Телефон"],
            ["group", "Группа"],
            ["consent", "Согласие"],
            ["blocked", "Запрет"],
          ]}
          actions={(r) => (
            <>
              <button
                disabled={busy}
                onClick={() =>
                  void action(async () =>
                    setHistory(
                      await api(`/workspace/contacts/${r.id}/history`),
                    ),
                  )
                }
              >
                История
              </button>
              {admin && (
                <>
                  <button onClick={() => setEditing(r)}>Изменить</button>
                  <button
                    className="danger"
                    disabled={busy}
                    onClick={() => {
                      const outcome = prompt(
                        "Причина жалобы / запрета звонков",
                      );
                      if (outcome)
                        void action(async () => {
                          await api(`/workspace/contacts/${r.id}/complaint`, {
                            method: "POST",
                            body: JSON.stringify({ outcome }),
                          });
                          await reload();
                          setNotice(
                            "Жалоба сохранена. Звонки контакту запрещены.",
                          );
                        });
                    }}
                  >
                    Жалоба
                  </button>
                  <button
                    className="danger"
                    disabled={busy}
                    onClick={() => {
                      if (
                        confirm(
                          "Архивировать контакт и запретить новые звонки? История сохранится.",
                        )
                      )
                        void action(async () => {
                          await api(`/workspace/contacts/${r.id}`, {
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
          total={contacts.data?.total ?? 0}
          onChange={setOffset}
        />
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
                await api(
                  `/workspace/contacts${list ? `?list_id=${list}` : ""}`,
                  {
                    method: "POST",
                    body: JSON.stringify({
                      name: data.get("name"),
                      phone: data.get("phone"),
                      group: data.get("group"),
                      email: data.get("email"),
                      notes: "",
                      consent,
                      consent_source: data.get("consent_source"),
                      crm_contact_id: data.get("crm_contact_id")
                        ? Number(data.get("crm_contact_id"))
                        : null,
                    }),
                  },
                );
                form.reset();
                setConsent(false);
                setNotice("Контакт добавлен.");
                await reload();
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
                <input
                  name="phone"
                  type="tel"
                  placeholder="+79991234567"
                  required
                />
                <small>Международный формат с +, например +79991234567.</small>
              </label>
              <label>
                Группа
                <input name="group" maxLength={120} />
              </label>
              <label>
                Email
                <input name="email" type="email" />
              </label>
              <label>
                Источник согласия
                <input
                  name="consent_source"
                  required={consent}
                  maxLength={256}
                />
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
      {admin && (
        <section>
          <h2>Базы и импорт контактов</h2>
          <p>
            CSV/XLSX с заголовками phone;name;group;email или один телефон в
            строке. До 1000 строк за импорт. Дубликаты сохраняют существующий
            запрет и согласие.
          </p>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              const f = e.currentTarget;
              const name = new FormData(f).get("name");
              void action(async () => {
                await api("/workspace/lists", {
                  method: "POST",
                  body: JSON.stringify({ name }),
                });
                f.reset();
                lists.reload();
              });
            }}
          >
            <div className="toolbar">
              <label>
                Название новой базы
                <input name="name" required />
              </label>
              <button disabled={busy}>Создать базу</button>
              {list && (
                <button
                  type="button"
                  className="danger"
                  onClick={() => {
                    if (confirm("Удалить базу? Контакты останутся."))
                      void action(async () => {
                        await api(`/workspace/lists/${list}`, {
                          method: "DELETE",
                        });
                        setList("");
                        lists.reload();
                      });
                  }}
                >
                  Удалить выбранную базу
                </button>
              )}
            </div>
          </form>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void action(async () => {
                const upload = new FormData();
                if (importFile) {
                  upload.set("file", importFile);
                  upload.set("consent", String(importConsent));
                  upload.set("consent_source", source);
                  if (list) upload.set("list_id", list);
                }
                const result = await api<{
                  created: number;
                  existing: number;
                  rejected: { line: number }[];
                }>(
                  importFile
                    ? "/workspace/contacts-import-file"
                    : "/workspace/contacts-import",
                  {
                    method: "POST",
                    body: importFile
                      ? upload
                      : JSON.stringify({
                          text: importText,
                          list_id: list || null,
                          consent: importConsent,
                          consent_source: source,
                        }),
                  },
                );
                setNotice(
                  `Импорт: добавлено ${result.created}, существующих ${result.existing}, отклонено ${result.rejected.length}${result.rejected.length ? `. Строки: ${result.rejected.map((r) => r.line).join(", ")}` : ""}.`,
                );
                await reload();
              });
            }}
          >
            <label>
              Файл CSV / XLSX (до 5 МБ)
              <input
                type="file"
                accept=".csv,.txt,.xlsx"
                onChange={(e) => {
                  const f = e.target.files?.[0];
                  if (f) {
                    if (f.size > 5 * 1024 * 1024) {
                      setNotice("Файл превышает 5 МБ. Разделите импорт.");
                      return;
                    }
                    setImportFile(f);
                    setImportText("");
                  }
                }}
              />
            </label>
            {importFile && (
              <p>
                Выбран файл: {importFile.name}.{" "}
                <button
                  type="button"
                  onClick={(e) => {
                    setImportFile(null);
                    const input =
                      e.currentTarget.form?.querySelector<HTMLInputElement>(
                        'input[type="file"]',
                      );
                    if (input) input.value = "";
                  }}
                >
                  Ввести текст вместо файла
                </button>
              </p>
            )}
            <label>
              Контакты для импорта
              <textarea
                value={importText}
                disabled={Boolean(importFile)}
                onChange={(e) => setImportText(e.target.value)}
                required={!importFile}
                rows={5}
              />
            </label>
            <label className="check">
              <input
                type="checkbox"
                checked={importConsent}
                onChange={(e) => setImportConsent(e.target.checked)}
              />
              Есть согласие для импортируемых контактов
            </label>
            <label>
              Источник согласия импорта
              <input
                value={source}
                onChange={(e) => setSource(e.target.value)}
                required={importConsent}
              />
            </label>
            <button disabled={busy}>Импортировать</button>
          </form>
        </section>
      )}
      {editing && (
        <Modal title="Редактировать контакт" close={() => setEditing(null)}>
          <Editor
            fields={editFields}
            initial={editing}
            busy={busy}
            submit={(v) =>
              action(async () => {
                await api(`/workspace/contacts/${editing.id}`, {
                  method: "PUT",
                  body: JSON.stringify(v),
                });
                setEditing(null);
                await reload();
              })
            }
          />
        </Modal>
      )}
      {history && (
        <Modal
          title={`История: ${String(history.contact.name)}`}
          close={() => setHistory(null)}
        >
          <RowsTable
            rows={history.calls}
            columns={[
              ["created_at", "Время"],
              ["campaign_name", "Кампания"],
              ["status", "Результат"],
              ["reason", "Причина"],
            ]}
          />
        </Modal>
      )}
    </>
  );
}
