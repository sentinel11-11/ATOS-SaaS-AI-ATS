import { useState } from "react";
import { api } from "./api";
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
  WorkspaceProps,
} from "./workspace";

const numberFields: Field[] = [
  { name: "phone", label: "Caller ID · 10 цифр", required: true },
  { name: "label", label: "Название" },
  { name: "enabled", label: "Использовать в обзвоне", type: "checkbox" },
  { name: "verified", label: "Номер разрешён оператором", type: "checkbox" },
  { name: "quarantined", label: "Карантин", type: "checkbox" },
  {
    name: "daily_limit",
    label: "Лимит попыток в сутки",
    type: "number",
    min: 1,
    max: 100000,
    required: true,
  },
  {
    name: "cooldown_seconds",
    label: "Пауза между вызовами, секунд",
    type: "number",
    min: 0,
    max: 86400,
    required: true,
  },
];
export function NumbersView(props: WorkspaceProps) {
  return (
    <ResourceEditor
      {...props}
      path="numbers"
      title="Пул Caller ID"
      fields={numberFields}
      defaults={{
        enabled: true,
        verified: false,
        quarantined: false,
        daily_limit: 30,
        cooldown_seconds: 60,
      }}
      columns={[
        ["phone", "Номер"],
        ["label", "Название"],
        ["enabled", "Включён"],
        ["verified", "Разрешён оператором"],
        ["quarantined", "Карантин"],
        ["daily_limit", "Лимит"],
        ["last_used_at", "Последний вызов"],
      ]}
      description="Для исходящих Мультиком использует Caller ID из 10 цифр. В ротации участвуют только разрешённые номера вне карантина, с учётом пауз и суточных лимитов."
    />
  );
}
export function BlacklistView(props: WorkspaceProps) {
  return (
    <ResourceEditor
      {...props}
      path="blacklist"
      title="Чёрный список"
      paged
      fields={[
        {
          name: "phone",
          label: "Телефон в международном формате",
          required: true,
        },
        { name: "reason", label: "Причина / жалоба", required: true },
      ]}
      columns={[
        ["phone", "Телефон"],
        ["reason", "Причина"],
        ["created_at", "Добавлен"],
      ]}
      description="Запрет применяется к существующим и новым контактам. Удаление записи из списка не восстанавливает отозванное согласие."
    />
  );
}
function ResourceEditor({
  path,
  title,
  fields,
  columns,
  defaults = {},
  description,
  paged = false,
  ...props
}: WorkspaceProps & {
  path: string;
  title: string;
  fields: Field[];
  columns: [string, string][];
  defaults?: Record<string, unknown>;
  description: string;
  paged?: boolean;
}) {
  const [offset, setOffset] = useState(0);
  const [q, setQ] = useState("");
  const r = useResource<Row[] | Page<Row>>(
    `/${path}${paged ? `?${new URLSearchParams({ q, offset: String(offset) })}` : ""}`,
  );
  const [edit, setEdit] = useState<Row | Record<string, unknown> | null>(null);
  const rows = paged
    ? ((r.data as Page<Row> | null)?.items ?? [])
    : ((r.data as Row[]) ?? []);
  return (
    <section>
      <div className="toolbar">
        <h2>{title}</h2>
        {props.admin && (
          <button onClick={() => setEdit(defaults)}>Добавить</button>
        )}
      </div>
      <p>{description}</p>
      {paged && (
        <input
          aria-label="Поиск в списке"
          value={q}
          placeholder="Поиск по телефону"
          onChange={(e) => {
            setQ(e.target.value);
            setOffset(0);
          }}
        />
      )}
      <LoadError error={r.error} />
      <RowsTable
        rows={rows}
        columns={columns}
        actions={
          props.admin
            ? (row) => (
                <>
                  {path !== "blacklist" && (
                    <button onClick={() => setEdit(row)}>Изменить</button>
                  )}
                  <button
                    className="danger"
                    disabled={props.busy}
                    onClick={() => {
                      if (confirm("Удалить запись?"))
                        void props.action(async () => {
                          await api(`/workspace/${path}/${row.id}`, {
                            method: "DELETE",
                          });
                          r.reload();
                        });
                    }}
                  >
                    Удалить
                  </button>
                </>
              )
            : undefined
        }
      />
      {paged && (
        <Pager
          offset={offset}
          total={(r.data as Page<Row> | null)?.total ?? 0}
          onChange={setOffset}
        />
      )}
      {edit && (
        <Modal
          title={edit.id ? "Изменить запись" : "Добавить запись"}
          close={() => setEdit(null)}
        >
          <Editor
            fields={fields}
            initial={edit}
            busy={props.busy}
            submit={(v) =>
              props.action(async () => {
                await api(`/workspace/${path}${edit.id ? `/${edit.id}` : ""}`, {
                  method: edit.id ? "PUT" : "POST",
                  body: JSON.stringify(v),
                });
                setEdit(null);
                r.reload();
                props.setNotice("Сохранено.");
              })
            }
          />
        </Modal>
      )}
    </section>
  );
}

type Question = {
  id: string;
  text: string;
  choices: Record<string, { next: string; qualified: boolean | null }>;
};
type Scenario = {
  greeting: string;
  intro: string;
  questions: Question[];
  default_qualified: boolean;
  qualified_text: string;
  not_qualified_text: string;
  goodbye: string;
};
const emptyScenario = (): Scenario => ({
  greeting: "Здравствуйте, {name}!",
  intro: "",
  questions: [],
  default_qualified: false,
  qualified_text: "Спасибо, передаю информацию специалисту.",
  not_qualified_text: "Спасибо, до свидания.",
  goodbye: "",
});
export function TemplatesView({
  admin,
  busy,
  action,
  setNotice,
}: WorkspaceProps) {
  const r = useResource<Row[]>("/templates");
  const [editing, setEditing] = useState<Row | null>(null);
  const [scenario, setScenario] = useState<Scenario>(emptyScenario);
  const [test, setTest] = useState<Row | null>(null);
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [result, setResult] = useState<{
    qualified: boolean;
    transcript: { speaker: string; text: string }[];
  } | null>(null);
  function open(row: Row) {
    setEditing(row);
    setScenario({ ...emptyScenario(), ...(row.scenario as Partial<Scenario>) });
  }
  function question(index: number, change: Partial<Question>) {
    setScenario((s) => ({
      ...s,
      questions: s.questions.map((q, i) =>
        i === index ? { ...q, ...change } : q,
      ),
    }));
  }
  return (
    <section>
      <div className="toolbar">
        <h2>Шаблоны и сценарии</h2>
        {admin && (
          <button
            onClick={() =>
              open({
                id: "",
                name: "Новый сценарий",
                text: "",
                scenario: emptyScenario(),
              })
            }
          >
            Новый шаблон
          </button>
        )}
      </div>
      <LoadError error={r.error} />
      <RowsTable
        rows={r.data ?? []}
        columns={[
          ["name", "Название"],
          ["text", "Сообщение"],
        ]}
        actions={(row) => (
          <>
            <button
              onClick={() => {
                setTest(row);
                setAnswers({});
                setResult(null);
              }}
            >
              Тест сценария
            </button>
            {admin && (
              <>
                <button onClick={() => open(row)}>Редактировать</button>
                <button
                  className="danger"
                  disabled={busy}
                  onClick={() => {
                    if (confirm("Архивировать шаблон?"))
                      void action(async () => {
                        await api(`/workspace/templates/${row.id}`, {
                          method: "DELETE",
                        });
                        r.reload();
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
      {editing && (
        <Modal title="Редактор сценария" close={() => setEditing(null)}>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              const data = new FormData(e.currentTarget);
              void action(async () => {
                await api(
                  `/workspace/templates${editing.id ? `/${editing.id}` : ""}`,
                  {
                    method: editing.id ? "PUT" : "POST",
                    body: JSON.stringify({
                      name: data.get("name"),
                      text: data.get("text"),
                      scenario,
                    }),
                  },
                );
                setEditing(null);
                r.reload();
                setNotice("Шаблон сохранён.");
              });
            }}
          >
            <div className="fields">
              <label>
                Название
                <input
                  name="name"
                  defaultValue={String(editing.name)}
                  required
                />
              </label>
              <label>
                Текст сообщения
                <textarea
                  name="text"
                  defaultValue={String(editing.text ?? "")}
                  rows={3}
                />
              </label>
              <label>
                Приветствие
                <input
                  value={scenario.greeting}
                  onChange={(e) =>
                    setScenario((s) => ({ ...s, greeting: e.target.value }))
                  }
                />
              </label>
              <label>
                Вступление
                <textarea
                  value={scenario.intro}
                  onChange={(e) =>
                    setScenario((s) => ({ ...s, intro: e.target.value }))
                  }
                />
              </label>
            </div>
            <p>
              Подстановка имени: {"{name}"}. Для каждого ответа можно выбрать
              следующий вопрос и результат квалификации.
            </p>
            {scenario.questions.map((q, index) => (
              <fieldset key={q.id}>
                <legend>
                  Вопрос {index + 1} · {q.id}
                </legend>
                <label>
                  Текст вопроса
                  <input
                    value={q.text}
                    required
                    onChange={(e) => question(index, { text: e.target.value })}
                  />
                </label>
                {Object.entries(q.choices).map(([key, c]) => (
                  <div className="fields" key={key}>
                    <label>
                      Ответ {key} →
                      <select
                        value={c.next}
                        onChange={(e) =>
                          question(index, {
                            choices: {
                              ...q.choices,
                              [key]: { ...c, next: e.target.value },
                            },
                          })
                        }
                      >
                        <option value="continue">Следующий вопрос</option>
                        <option value="end">Завершить</option>
                        {scenario.questions.map((target) => (
                          <option key={target.id} value={target.id}>
                            {target.id}: {target.text.slice(0, 40)}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label>
                      Квалификация
                      <select
                        value={
                          c.qualified === null ? "keep" : String(c.qualified)
                        }
                        onChange={(e) =>
                          question(index, {
                            choices: {
                              ...q.choices,
                              [key]: {
                                ...c,
                                qualified:
                                  e.target.value === "keep"
                                    ? null
                                    : e.target.value === "true",
                              },
                            },
                          })
                        }
                      >
                        <option value="keep">Без изменений</option>
                        <option value="true">Подходит</option>
                        <option value="false">Не подходит</option>
                      </select>
                    </label>
                  </div>
                ))}
                <button
                  type="button"
                  className="danger"
                  onClick={() =>
                    setScenario((s) => ({
                      ...s,
                      questions: s.questions
                        .filter((_, i) => i !== index)
                        .map((item) => ({
                          ...item,
                          choices: Object.fromEntries(
                            Object.entries(item.choices).map(([k, c]) => [
                              k,
                              c.next === q.id ? { ...c, next: "end" } : c,
                            ]),
                          ),
                        })),
                    }))
                  }
                >
                  Удалить вопрос
                </button>
              </fieldset>
            ))}
            <button
              type="button"
              onClick={() =>
                setScenario((s) => ({
                  ...s,
                  questions: [
                    ...s.questions,
                    {
                      id: crypto.randomUUID().slice(0, 8),
                      text: "Вам интересно предложение?",
                      choices: {
                        "1": { next: "end", qualified: true },
                        "2": { next: "end", qualified: false },
                      },
                    },
                  ],
                }))
              }
            >
              Добавить вопрос
            </button>
            <div className="fields">
              <label>
                Ответ при квалификации
                <textarea
                  value={scenario.qualified_text}
                  onChange={(e) =>
                    setScenario((s) => ({
                      ...s,
                      qualified_text: e.target.value,
                    }))
                  }
                />
              </label>
              <label>
                Ответ при отказе
                <textarea
                  value={scenario.not_qualified_text}
                  onChange={(e) =>
                    setScenario((s) => ({
                      ...s,
                      not_qualified_text: e.target.value,
                    }))
                  }
                />
              </label>
              <label>
                Прощание
                <input
                  value={scenario.goodbye}
                  onChange={(e) =>
                    setScenario((s) => ({ ...s, goodbye: e.target.value }))
                  }
                />
              </label>
            </div>
            <label className="check">
              <input
                type="checkbox"
                checked={scenario.default_qualified}
                onChange={(e) =>
                  setScenario((s) => ({
                    ...s,
                    default_qualified: e.target.checked,
                  }))
                }
              />
              Квалификация до ответов
            </label>
            <button disabled={busy}>Сохранить шаблон</button>
          </form>
        </Modal>
      )}
      {test && (
        <Modal title="Тест сценария без звонка" close={() => setTest(null)}>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void action(async () =>
                setResult(
                  await api(`/workspace/templates/${test.id}/test`, {
                    method: "POST",
                    body: JSON.stringify({ answers }),
                  }),
                ),
              );
            }}
          >
            {((test.scenario as Scenario)?.questions ?? []).map((q) => (
              <label key={q.id}>
                {q.text}
                <input
                  value={answers[q.id] ?? ""}
                  onChange={(e) =>
                    setAnswers((a) => ({ ...a, [q.id]: e.target.value }))
                  }
                  placeholder={Object.keys(q.choices).join(" / ")}
                />
              </label>
            ))}
            <button disabled={busy}>Проверить сценарий</button>
          </form>
          {result && (
            <>
              <p>
                Квалифицирован: <b>{result.qualified ? "Да" : "Нет"}</b>
              </p>
              {result.transcript.map((t, i) => (
                <p key={i}>
                  <b>{t.speaker === "agent" ? "Агент" : "Абонент"}:</b> {t.text}
                </p>
              ))}
            </>
          )}
        </Modal>
      )}
    </section>
  );
}
