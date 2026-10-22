import { labels } from "./types";
import type { Attempt } from "./types";
export function CallsView({ attempts }: { attempts: Attempt[] }) {
  return (
    <section>
      <h2>Последние 100 попыток</h2>
      <p>
        «Требует сверки» означает неизвестный исход. Повторный вызов
        автоматически не выполняется.
      </p>
      <table>
        <thead>
          <tr>
            <th>Время</th>
            <th>Идентификатор</th>
            <th>Маршрут</th>
            <th>Состояние</th>
            <th>Причина</th>
          </tr>
        </thead>
        <tbody>
          {attempts.map((a) => (
            <tr key={a.id}>
              <td>
                {new Date(a.created_at).toLocaleString("ru-RU", {
                  timeZone: "Europe/Moscow",
                })}
              </td>
              <td className="mono">{a.id}</td>
              <td>{labels[a.route]}</td>
              <td>{labels[a.status]}</td>
              <td>{a.reason || "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
