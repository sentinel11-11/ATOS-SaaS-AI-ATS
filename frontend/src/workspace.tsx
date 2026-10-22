import { FormEvent, ReactNode, useEffect, useState } from "react";
import { api, ApiError } from "./api";

export type Row = { id: string; [key: string]: unknown };
export type Page<T> = { items: T[]; total: number };
export type WorkspaceProps = {
  admin: boolean;
  role?: string;
  userId?: string;
  action: (fn: () => Promise<void>) => Promise<void>;
  busy: boolean;
  setNotice: (text: string) => void;
};
export const describeError = (e: unknown) =>
  e instanceof ApiError
    ? `${e.message}${e.requestId ? ` · ${e.requestId}` : ""}`
    : "Нет связи с сервером.";
export const display = (v: unknown): string =>
  v === null || v === undefined || v === ""
    ? "—"
    : typeof v === "boolean"
      ? v
        ? "Да"
        : "Нет"
      : typeof v === "object"
        ? JSON.stringify(v)
        : String(v);
export const dateTime = (v: unknown) =>
  v
    ? new Date(String(v)).toLocaleString("ru-RU", { timeZone: "Europe/Moscow" })
    : "—";

export function useResource<T>(path: string | null) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState("");
  const [version, setVersion] = useState(0);
  useEffect(() => {
    if (!path) {
      setData(null);
      setError("");
      return;
    }
    const controller = new AbortController();
    let pending = false;
    setData(null);
    async function load() {
      if (pending) return;
      pending = true;
      try {
        const result = await api<T>(`/workspace${path}`, {
          signal: controller.signal,
        });
        if (!controller.signal.aborted) {
          setData(result);
          setError("");
        }
      } catch (e) {
        if (!controller.signal.aborted) setError(describeError(e));
      } finally {
        pending = false;
      }
    }
    void load();
    const timer = setInterval(() => void load(), 5000);
    return () => {
      controller.abort();
      clearInterval(timer);
    };
  }, [path, version]);
  return { data, error, reload: () => setVersion((v) => v + 1) };
}

export function LoadError({ error }: { error: string }) {
  return error ? (
    <p role="alert" className="alert">
      {error}
    </p>
  ) : null;
}
export function Empty({ children }: { children?: ReactNode }) {
  return <p className="empty">{children ?? "Записей пока нет."}</p>;
}
export function Pager({
  offset,
  total,
  onChange,
}: {
  offset: number;
  total: number;
  onChange: (n: number) => void;
}) {
  return (
    <div className="toolbar">
      <button
        disabled={!offset}
        onClick={() => onChange(Math.max(0, offset - 50))}
      >
        Назад
      </button>
      <span>
        {total
          ? `${offset + 1}–${Math.min(offset + 50, total)} из ${total}`
          : "0 записей"}
      </span>
      <button
        disabled={offset + 50 >= total}
        onClick={() => onChange(offset + 50)}
      >
        Далее
      </button>
    </div>
  );
}
export function Modal({
  title,
  children,
  close,
}: {
  title: string;
  children: ReactNode;
  close: () => void;
}) {
  return (
    <div className="modal-backdrop">
      <section
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className="modal"
      >
        <div className="toolbar">
          <h2>{title}</h2>
          <button type="button" className="secondary" onClick={close}>
            Закрыть
          </button>
        </div>
        {children}
      </section>
    </div>
  );
}
export type Field = {
  name: string;
  label: string;
  type?:
    | "text"
    | "number"
    | "checkbox"
    | "textarea"
    | "password"
    | "select"
    | "weekdays";
  required?: boolean;
  options?: [string, string][];
  min?: number;
  max?: number;
  hint?: string;
};
export function Editor({
  fields,
  initial = {},
  submit,
  busy,
}: {
  fields: Field[];
  initial?: Record<string, unknown>;
  submit: (v: Record<string, unknown>) => Promise<void>;
  busy: boolean;
}) {
  async function save(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = new FormData(e.currentTarget);
    const values: Record<string, unknown> = {};
    for (const f of fields)
      values[f.name] =
        f.type === "weekdays"
          ? form.getAll(f.name).map(Number)
          : f.type === "checkbox"
            ? form.has(f.name)
            : f.type === "number"
              ? form.get(f.name) === ""
                ? null
                : Number(form.get(f.name))
              : String(form.get(f.name) ?? "");
    await submit(values);
  }
  return (
    <form onSubmit={(e) => void save(e)}>
      <div className="fields">
        {fields.map((f) =>
          f.type === "weekdays" ? (
            <fieldset key={f.name}>
              <legend>{f.label}</legend>
              {["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"].map((day, index) => (
                <label className="check" key={day}>
                  <input
                    type="checkbox"
                    name={f.name}
                    value={index}
                    defaultChecked={(
                      (initial[f.name] as number[] | undefined) ?? [
                        0, 1, 2, 3, 4, 5, 6,
                      ]
                    ).includes(index)}
                  />
                  {day}
                </label>
              ))}
            </fieldset>
          ) : (
            <label
              key={f.name}
              className={f.type === "checkbox" ? "check" : ""}
            >
              {f.label}
              {f.type === "textarea" ? (
                <textarea
                  name={f.name}
                  defaultValue={String(initial[f.name] ?? "")}
                  required={f.required}
                  rows={5}
                />
              ) : f.type === "select" ? (
                <select
                  name={f.name}
                  defaultValue={String(
                    initial[f.name] ?? f.options?.[0]?.[0] ?? "",
                  )}
                >
                  {f.options?.map(([v, t]) => (
                    <option key={v} value={v}>
                      {t}
                    </option>
                  ))}
                </select>
              ) : f.type === "checkbox" ? (
                <input
                  name={f.name}
                  type="checkbox"
                  defaultChecked={Boolean(initial[f.name])}
                />
              ) : (
                <input
                  name={f.name}
                  type={f.type ?? "text"}
                  defaultValue={String(initial[f.name] ?? "")}
                  required={f.required}
                  min={f.min}
                  max={f.max}
                />
              )}
              {f.hint && <small>{f.hint}</small>}
            </label>
          ),
        )}
      </div>
      <button disabled={busy}>Сохранить</button>
    </form>
  );
}
export function RowsTable({
  rows,
  columns,
  actions,
}: {
  rows: Row[];
  columns: [string, string][];
  actions?: (r: Row) => ReactNode;
}) {
  if (!rows.length) return <Empty />;
  return (
    <table>
      <thead>
        <tr>
          {columns.map(([key, label]) => (
            <th key={key}>{label}</th>
          ))}
          {actions && <th>Действия</th>}
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.id}>
            {columns.map(([key]) => (
              <td key={key}>
                {key.endsWith("_at") ? dateTime(r[key]) : display(r[key])}
              </td>
            ))}
            {actions && (
              <td>
                <div className="row-actions">{actions(r)}</div>
              </td>
            )}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export async function downloadCsv(kind: "contacts" | "calls", query = "") {
  const response = await fetch(
    `/api/v1/workspace/export/${kind}.csv${query ? `?${query}` : ""}`,
    { credentials: "same-origin" },
  );
  if (!response.ok) throw new ApiError(response.status, "export_failed");
  const url = URL.createObjectURL(await response.blob());
  const link = document.createElement("a");
  link.href = url;
  link.download = `${kind}.csv`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
