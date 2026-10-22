import { useState } from "react";
import { api } from "./api";
import { labels } from "./types";
import {
  dateTime,
  display,
  downloadCsv,
  Editor,
  LoadError,
  Modal,
  Page,
  Pager,
  Row,
  RowsTable,
  useResource,
  WorkspaceProps,
} from "./workspace";

type Detail = { call: Row; events: Row[]; crm: Row[]; recordings: Row[] };
export function CallsView({ admin, busy, action, setNotice }: WorkspaceProps) {
  const [q, setQ] = useState("");
  const [status, setStatus] = useState("");
  const [since, setSince] = useState("");
  const [until, setUntil] = useState("");
  const [offset, setOffset] = useState(0);
  const [detailId, setDetailId] = useState("");
  const [kind, setKind] = useState("calls");
  const [share, setShare] = useState<{
    id: string;
    url: string;
    expires_at: string;
  } | null>(null);
  const params = new URLSearchParams({
    q,
    status,
    offset: String(offset),
    ...(since ? { since: new Date(since).toISOString() } : {}),
    ...(until ? { until: new Date(until).toISOString() } : {}),
  });
  const calls = useResource<Page<Row>>(`/${kind}?${params}`);
  const detail = useResource<Detail>(detailId ? `/calls/${detailId}` : null);
  return (
    <>
      <section>
        <h2>Журнал звонков</h2>
        <p>
          «Требует сверки» означает неизвестный исход. Повторный вызов
          автоматически не выполняется.
        </p>
        <div className="toolbar">
          <select
            aria-label="Раздел журнала"
            value={kind}
            onChange={(e) => {
              setKind(e.target.value);
              setOffset(0);
              setDetailId("");
            }}
          >
            <option value="calls">Кампании</option>
            <option value="incoming">Входящие</option>
            <option value="history">История старой ATS</option>
          </select>
          <input
            aria-label="Поиск звонков"
            placeholder="Имя или телефон"
            value={q}
            onChange={(e) => {
              setQ(e.target.value);
              setOffset(0);
            }}
          />
          <select
            aria-label="Состояние звонка"
            value={status}
            onChange={(e) => {
              setStatus(e.target.value);
              setOffset(0);
            }}
          >
            <option value="">Все состояния</option>
            {[
              "prepared",
              "dispatching",
              "accepted",
              "answered",
              "completed",
              "failed",
              "unknown",
            ].map((s) => (
              <option value={s} key={s}>
                {labels[s]}
              </option>
            ))}
          </select>
          {admin && kind === "calls" && (
            <button
              onClick={() =>
                void action(() => downloadCsv("calls", params.toString()))
              }
            >
              Экспорт CSV
            </button>
          )}
        </div>
        <div className="fields">
          <label>
            Период с
            <input
              type="datetime-local"
              value={since}
              onChange={(e) => {
                setSince(e.target.value);
                setOffset(0);
              }}
            />
          </label>
          <label>
            До (не включая)
            <input
              type="datetime-local"
              value={until}
              onChange={(e) => {
                setUntil(e.target.value);
                setOffset(0);
              }}
            />
          </label>
        </div>
        <LoadError error={calls.error} />
        <RowsTable
          rows={(calls.data?.items ?? []).map((r) => ({
            ...r,
            status_label: labels[String(r.status)] ?? r.status,
            route_label: labels[String(r.route)] ?? r.route,
          }))}
          columns={[
            ["created_at", "Время"],
            ["contact_name", "Контакт"],
            ["phone", "Телефон"],
            ["campaign_name", "Кампания"],
            ["route_label", "Маршрут"],
            ["status_label", "Состояние"],
            ["reason", "Причина"],
          ]}
          actions={
            kind === "calls"
              ? (r) => (
                  <button onClick={() => setDetailId(r.id)}>Подробности</button>
                )
              : undefined
          }
        />
        <Pager
          offset={offset}
          total={calls.data?.total ?? 0}
          onChange={setOffset}
        />
      </section>
      {detailId && (
        <Modal title="Карточка звонка" close={() => setDetailId("")}>
          <LoadError error={detail.error} />
          {detail.data && (
            <>
              {admin &&
                [
                  "prepared",
                  "dispatching",
                  "accepted",
                  "answered",
                  "unknown",
                ].includes(String(detail.data.call.status)) &&
                (detail.data.call.route === "sim" ||
                  detail.data.call.route === "multicom_ami" ||
                  detail.data.call.status === "prepared") && (
                  <button
                    className="danger"
                    disabled={busy}
                    onClick={() => {
                      if (confirm("Завершить этот вызов?"))
                        void action(async () => {
                          await api(`/workspace/calls/${detailId}/hangup`, {
                            method: "POST",
                          });
                          detail.reload();
                          calls.reload();
                          setNotice(
                            "Запрос на завершение обработан. Итог от АТС появится в журнале.",
                          );
                        });
                    }}
                  >
                    Завершить вызов
                  </button>
                )}
              <dl className="detail-grid">
                {[
                  ["Контакт", detail.data.call.contact_name],
                  ["Телефон", detail.data.call.phone],
                  ["Кампания", detail.data.call.campaign_name],
                  ["Состояние", labels[String(detail.data.call.status)]],
                  ["Время", dateTime(detail.data.call.created_at)],
                  ["Длительность, сек.", detail.data.call.duration_seconds],
                  ["Caller ID", detail.data.call.caller_number],
                  ["Причина", detail.data.call.reason],
                  ["Внешний ID", detail.data.call.external_id],
                ].map(([k, v]) => (
                  <div key={String(k)}>
                    <dt>{String(k)}</dt>
                    <dd>{display(v)}</dd>
                  </div>
                ))}
              </dl>
              <h3>Этапы обработки</h3>
              <RowsTable
                rows={detail.data.events.map((e) => ({
                  ...e,
                  event_label: labels[String(e.event)] ?? e.event,
                }))}
                columns={[
                  ["created_at", "Время"],
                  ["event_label", "Этап"],
                  ["detail", "Описание"],
                ]}
              />
              <h3>Доставка в CRM</h3>
              <RowsTable
                rows={detail.data.crm}
                columns={[
                  ["status", "Состояние"],
                  ["attempts", "Попытки"],
                  ["last_error", "Причина"],
                ]}
              />
              <h3>Записи разговора</h3>
              {admin &&
                detail.data.recordings.map((r) => (
                  <button
                    key={r.id}
                    disabled={busy}
                    onClick={() =>
                      void action(async () =>
                        setShare(
                          await api(`/workspace/recordings/${r.id}/share`, {
                            method: "POST",
                          }),
                        ),
                      )
                    }
                  >
                    Ссылка на запись на 24 часа
                  </button>
                ))}
              {share && (
                <div>
                  <label>
                    Ссылка на одну запись · до {dateTime(share.expires_at)}
                    <input
                      readOnly
                      value={share.url}
                      onFocus={(e) => e.target.select()}
                    />
                  </label>
                  <button
                    className="danger"
                    onClick={() =>
                      void action(async () => {
                        await api(`/workspace/recording-shares/${share.id}`, {
                          method: "DELETE",
                        });
                        setShare(null);
                      })
                    }
                  >
                    Отозвать ссылку
                  </button>
                </div>
              )}
              {detail.data.recordings.map((r) => (
                <audio
                  key={r.id}
                  controls
                  preload="none"
                  src={`/api/v1/workspace/recordings/${r.id}`}
                />
              ))}
              {!detail.data.recordings.length && (
                <p>Запись разговора не загружена.</p>
              )}
              {admin && (
                <label>
                  Загрузить WAV или OGG (до 10 МБ)
                  <input
                    type="file"
                    accept=".wav,.ogg"
                    onChange={(e) => {
                      const file = e.target.files?.[0];
                      if (file)
                        void action(async () => {
                          const form = new FormData();
                          form.set("file", file);
                          await api(`/workspace/calls/${detailId}/recordings`, {
                            method: "POST",
                            body: form,
                          });
                          detail.reload();
                          setNotice("Запись загружена.");
                        });
                    }}
                  />
                </label>
              )}
              {detail.data.call.agent_result &&
                Object.keys(detail.data.call.agent_result as object).length >
                  0 && (
                  <>
                    <h3>Результат сценария</h3>
                    <pre>
                      {JSON.stringify(detail.data.call.agent_result, null, 2)}
                    </pre>
                  </>
                )}
              {admin && detail.data.call.status === "unknown" && (
                <>
                  <h3>Сверка неизвестного исхода</h3>
                  <p>
                    Укажите результат только после проверки у оператора.
                    Освобождение канала не завершает разговор на АТС.
                  </p>
                  <Editor
                    busy={busy}
                    fields={[
                      {
                        name: "status",
                        label: "Подтверждённый результат",
                        type: "select",
                        options: [
                          ["completed", "Завершено"],
                          ["failed", "Ошибка"],
                        ],
                      },
                      {
                        name: "evidence",
                        label: "Основание сверки, 10–80 символов",
                        required: true,
                      },
                    ]}
                    submit={(v) =>
                      action(async () => {
                        await api(`/attempts/${detailId}/resolve`, {
                          method: "POST",
                          body: JSON.stringify(v),
                        });
                        detail.reload();
                        calls.reload();
                      })
                    }
                  />
                </>
              )}
            </>
          )}
        </Modal>
      )}
    </>
  );
}
