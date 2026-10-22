import { useState } from "react";
import { api } from "./api";
import {
  display,
  Editor,
  Field,
  LoadError,
  Modal,
  Row,
  RowsTable,
  useResource,
  WorkspaceProps,
} from "./workspace";

const userFields: Field[] = [
  { name: "login", label: "Логин", required: true },
  {
    name: "password",
    label: "Новый пароль",
    type: "password",
    hint: "Минимум 12 символов. При редактировании оставьте пустым, чтобы сохранить пароль.",
  },
  {
    name: "role",
    label: "Роль",
    type: "select",
    options: [
      ["operator", "Оператор"],
      ["viewer", "Наблюдатель"],
      ["admin", "Администратор"],
    ],
  },
  { name: "active", label: "Учётная запись активна", type: "checkbox" },
  { name: "name", label: "Имя оператора" },
  { name: "extension", label: "Внутренний номер" },
];
export function UsersView({ busy, action, setNotice }: WorkspaceProps) {
  const r = useResource<Row[]>("/users");
  const [editing, setEditing] = useState<Row | null>(null);
  return (
    <section>
      <div className="toolbar">
        <h2>Пользователи и доступ</h2>
        <button
          onClick={() =>
            setEditing({ id: "", login: "", role: "operator", active: true })
          }
        >
          Добавить пользователя
        </button>
      </div>
      <LoadError error={r.error} />
      <RowsTable
        rows={r.data ?? []}
        columns={[
          ["login", "Логин"],
          ["role", "Роль"],
          ["active", "Активен"],
          ["name", "Имя"],
          ["extension", "Внутренний номер"],
        ]}
        actions={(row) => (
          <button onClick={() => setEditing(row)}>Изменить доступ</button>
        )}
      />
      {editing && (
        <Modal title="Учётная запись" close={() => setEditing(null)}>
          <Editor
            fields={userFields}
            initial={editing}
            busy={busy}
            submit={(v) =>
              action(async () => {
                await api(
                  `/workspace/users${editing.id ? `/${editing.id}` : ""}`,
                  {
                    method: editing.id ? "PUT" : "POST",
                    body: JSON.stringify(v),
                  },
                );
                setEditing(null);
                r.reload();
                setNotice(
                  "Доступ сохранён. Сессии изменённого пользователя отозваны.",
                );
              })
            }
          />
        </Modal>
      )}
    </section>
  );
}

type SettingsData = {
  tenant: Row;
  providers: Record<string, unknown>;
  revision: number;
  connections: Record<string, unknown>;
};
export function SettingsView({ busy, action, setNotice }: WorkspaceProps) {
  const r = useResource<SettingsData>("/settings");
  const directory = useResource<Row[]>("/directory");
  const [tenantEdit, setTenantEdit] = useState(false);
  const [providerEdit, setProviderEdit] = useState(false);
  const tenantFields: Field[] = [
    { name: "name", label: "Название пространства", required: true },
    { name: "timezone", label: "Часовой пояс", required: true },
    {
      name: "max_channels",
      label: "Максимум каналов",
      type: "number",
      min: 1,
      max: 100,
      required: true,
    },
    {
      name: "daily_limit",
      label: "Попыток в сутки",
      type: "number",
      min: 1,
      max: 100000,
      required: true,
    },
  ];
  const providerFields: Field[] = [
    {
      name: "auto_quarantine_on_complaints",
      label: "Жалоб до карантина Caller ID",
      type: "number",
      required: true,
      min: 1,
      max: 100,
    },
    { name: "megafon_user", label: "Пользователь МегаФон для callback" },
    {
      name: "amo_responsible_user_id",
      label: "Ответственный amoCRM (ID)",
      type: "number",
      min: 1,
    },
    {
      name: "amo_auto_create_contacts",
      label: "Создавать контакты в amoCRM",
      type: "checkbox",
    },
    {
      name: "amo_auto_create_tasks",
      label: "Создавать задачи после звонка",
      type: "checkbox",
    },
    {
      name: "task_due_hours",
      label: "Срок задачи, часов",
      type: "number",
      required: true,
      min: 1,
      max: 168,
    },
    {
      name: "default_mode",
      label: "Режим по умолчанию",
      type: "select",
      options: [
        ["message", "Сообщение"],
        ["agent", "Сценарий"],
        ["operator", "Операторская обработка"],
      ],
    },
  ];
  const connectionLabels: Record<string, string> = {
    ami_host: "AMI · адрес",
    ami_port: "AMI · порт",
    ami_user: "AMI · пользователь",
    ami_tls: "AMI · TLS",
    ami_secret_set: "Пароль AMI задан",
    caller_id: "Caller ID по умолчанию",
    megafon_url: "МегаФон · адрес",
    megafon_key_set: "Ключ МегаФон задан",
    amo_url: "amoCRM · адрес",
    amo_token_set: "Токен amoCRM задан",
    crm_enabled: "Доставка в CRM включена",
    live_calls_enabled: "Реальные вызовы разрешены",
    test_destinations: "Разрешённые тестовые номера",
  };
  return (
    <>
      <LoadError error={r.error} />
      <section>
        <div className="toolbar">
          <h2>Пространство и лимиты</h2>
          <button onClick={() => setTenantEdit(true)}>Настроить</button>
        </div>
        <dl className="detail-grid">
          {tenantFields.map((f) => (
            <div key={f.name}>
              <dt>{f.label}</dt>
              <dd>{display(r.data?.tenant[f.name])}</dd>
            </div>
          ))}
        </dl>
      </section>
      <section>
        <div className="toolbar">
          <h2>Телефония и CRM</h2>
          <button onClick={() => setProviderEdit(true)}>
            Настроить обработку
          </button>
        </div>
        <dl className="detail-grid">
          {Object.entries(r.data?.connections ?? {}).map(([key, value]) => (
            <div key={key}>
              <dt>{connectionLabels[key] ?? key}</dt>
              <dd>{display(value)}</dd>
            </div>
          ))}
        </dl>
        <p>
          Учётные данные подключений задаются в защищённой конфигурации сервера.
          Изменение рабочих ключей требует обновить конфигурацию API и воркеров.
        </p>
        <div className="toolbar">
          {[
            ["ami", "Проверить AMI"],
            ["megafon", "Проверить МегаФон ВАТС"],
            ["amocrm", "Проверить amoCRM"],
          ].map(([key, title]) => (
            <button
              key={key}
              disabled={busy}
              onClick={() =>
                void action(async () => {
                  const job = await api<{ job_id: string }>(
                    `/workspace/diagnostics/${key}`,
                    { method: "POST" },
                  );
                  setNotice(
                    `Проверка поставлена в очередь: ${job.job_id}. Результат появится в «Логи → Системные события». Звонки не совершаются.`,
                  );
                })
              }
            >
              {title}
            </button>
          ))}
        </div>
      </section>
      <section>
        <h2>Справочники интеграций</h2>
        <div className="toolbar">
          {[
            ["megafon", "users", "Сотрудники ВАТС"],
            ["megafon", "groups", "Группы ВАТС"],
            ["megafon", "numbers", "Номера ВАТС"],
            ["amocrm", "users", "Пользователи amoCRM"],
          ].map(([provider, kind, label]) => (
            <button
              key={provider + kind}
              disabled={busy}
              onClick={() =>
                void action(async () => {
                  const job = await api<{ job_id: string }>(
                    `/workspace/sync/${provider}/${kind}`,
                    { method: "POST" },
                  );
                  setNotice(
                    `Синхронизация поставлена в очередь: ${job.job_id}.`,
                  );
                  directory.reload();
                })
              }
            >
              {label}
            </button>
          ))}
        </div>
        <LoadError error={directory.error} />
        <RowsTable
          rows={directory.data ?? []}
          columns={[
            ["provider", "Источник"],
            ["kind", "Тип"],
            ["name", "Название"],
            ["phone", "Номер"],
            ["external_id", "ID"],
            ["updated_at", "Обновлён"],
          ]}
        />
      </section>
      {tenantEdit && r.data && (
        <Modal
          title="Настройки пространства"
          close={() => setTenantEdit(false)}
        >
          <Editor
            fields={tenantFields}
            initial={r.data.tenant}
            busy={busy}
            submit={(v) =>
              action(async () => {
                await api("/workspace/settings/tenant", {
                  method: "PUT",
                  body: JSON.stringify(v),
                });
                setTenantEdit(false);
                r.reload();
              })
            }
          />
        </Modal>
      )}
      {providerEdit && r.data && (
        <Modal
          title="Обработка звонков и CRM"
          close={() => setProviderEdit(false)}
        >
          <Editor
            fields={providerFields}
            initial={r.data.providers}
            busy={busy}
            submit={(v) =>
              action(async () => {
                await api("/workspace/settings/providers", {
                  method: "PUT",
                  body: JSON.stringify(v),
                });
                setProviderEdit(false);
                r.reload();
              })
            }
          />
        </Modal>
      )}
    </>
  );
}
export function OperatorsView({
  action,
  busy,
  role,
  userId,
  admin,
}: WorkspaceProps) {
  const r = useResource<{ operators: Row[]; queue: Row[] }>("/operators");
  return (
    <>
      <LoadError error={r.error} />
      <section>
        <h2>Операторы</h2>
        <RowsTable
          rows={r.data?.operators ?? []}
          columns={[
            ["login", "Логин"],
            ["name", "Имя"],
            ["extension", "Внутренний номер"],
            ["status", "Статус"],
          ]}
        />
        <div className="toolbar">
          {role === "operator" &&
            [
              ["free", "Готов к работе"],
              ["break", "Перерыв"],
              ["offline", "Не в сети"],
            ].map(([status, title]) => (
              <button
                key={status}
                disabled={busy}
                onClick={() =>
                  void action(async () => {
                    await api("/workspace/operator-status", {
                      method: "POST",
                      body: JSON.stringify({ status }),
                    });
                    r.reload();
                  })
                }
              >
                {title}
              </button>
            ))}
        </div>
      </section>
      <section>
        <h2>Очередь операторской обработки</h2>
        <p>
          Задачи по результатам звонков. Принятие задачи закрепляет её за
          оператором; соединение голосового канала требует отдельного сценария
          АТС.
        </p>
        <RowsTable
          rows={r.data?.queue ?? []}
          columns={[
            ["created_at", "Время"],
            ["contact_name", "Контакт"],
            ["phone", "Телефон"],
            ["attempt_id", "Звонок"],
            ["status", "Состояние"],
            ["operator_id", "Оператор"],
          ]}
          actions={(row) => (
            <>
              {row.status === "waiting" && role === "operator" && (
                <button
                  disabled={busy}
                  onClick={() =>
                    void action(async () => {
                      await api(`/workspace/queue/${row.id}/accept`, {
                        method: "POST",
                      });
                      r.reload();
                    })
                  }
                >
                  Взять в работу
                </button>
              )}
              {row.can_hangup &&
                (admin ||
                  r.data?.operators.some(
                    (op) => op.id === row.operator_id && op.user_id === userId,
                  )) && (
                  <button
                    className="danger"
                    disabled={busy}
                    onClick={() => {
                      if (confirm("Завершить этот вызов?"))
                        void action(async () => {
                          await api(
                            `/workspace/calls/${row.attempt_id}/hangup`,
                            { method: "POST" },
                          );
                          r.reload();
                        });
                    }}
                  >
                    Завершить вызов
                  </button>
                )}
              {row.status === "accepted" &&
                (row.route === "sim" ||
                  ![
                    "prepared",
                    "dispatching",
                    "accepted",
                    "answered",
                    "unknown",
                  ].includes(String(row.call_status))) &&
                (admin ||
                  r.data?.operators.some(
                    (op) => op.id === row.operator_id && op.user_id === userId,
                  )) && (
                  <button
                    disabled={busy}
                    onClick={() => {
                      const outcome = prompt("Результат обработки");
                      if (outcome)
                        void action(async () => {
                          await api(`/workspace/queue/${row.id}/complete`, {
                            method: "POST",
                            body: JSON.stringify({ outcome }),
                          });
                          r.reload();
                        });
                    }}
                  >
                    Завершить обработку
                  </button>
                )}
            </>
          )}
        />
      </section>
    </>
  );
}
export function PasswordView({ action, busy }: WorkspaceProps) {
  return (
    <section>
      <h2>Смена пароля</h2>
      <p>После смены пароля потребуется войти заново.</p>
      <Editor
        fields={[
          {
            name: "current_password",
            label: "Текущий пароль",
            type: "password",
            required: true,
          },
          {
            name: "new_password",
            label: "Новый пароль · минимум 12 символов",
            type: "password",
            required: true,
          },
        ]}
        busy={busy}
        submit={(v) =>
          action(async () => {
            await api("/workspace/password", {
              method: "POST",
              body: JSON.stringify(v),
            });
            location.reload();
          })
        }
      />
    </section>
  );
}
