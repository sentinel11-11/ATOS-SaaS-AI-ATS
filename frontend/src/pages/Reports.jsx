import React, { useEffect, useMemo, useState } from 'react';
import { BarChart3, Clock3, Download, Hash, PhoneCall, Users } from 'lucide-react';
import { api, openWithTicket } from '../api';

const localDate = (value) => {
  const date = new Date(value);
  const offset = date.getTimezoneOffset() * 60000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 10);
};

function periodRange(kind) {
  const today = new Date();
  const end = localDate(today);
  const start = new Date(today);
  if (kind === 'week') start.setDate(today.getDate() - 6);
  if (kind === 'month') start.setDate(today.getDate() - 29);
  return { from: localDate(start), to: end };
}

const tabs = [
  { id: 'count', label: 'Количество звонков', icon: BarChart3 },
  { id: 'direction', label: 'Направление звонков', icon: PhoneCall },
  { id: 'employees', label: 'Рейтинг сотрудников', icon: Users },
  { id: 'numbers', label: 'Статистика по номерам', icon: Hash },
  { id: 'hours', label: 'Нагрузка по часам', icon: Clock3 },
];

const number = (value) => Number(value || 0).toLocaleString('ru-RU');

function Empty({ children = 'Нет данных за выбранный период' }) {
  return <div className="py-12 text-center text-dim text-sm">{children}</div>;
}

export default function Reports({ addToast, refreshKey }) {
  const [range, setRange] = useState(() => periodRange('week'));
  const [periodKind, setPeriodKind] = useState('week');
  const [activeTab, setActiveTab] = useState('count');
  const [report, setReport] = useState(null);
  const [loading, setLoading] = useState(true);

  const loadReport = async () => {
    setLoading(true);
    const query = `?from=${encodeURIComponent(range.from)}&to=${encodeURIComponent(range.to)}`;
    const result = await api(`/analytics${query}`);
    if (result?.ok) setReport(result);
    else addToast?.(`Не удалось загрузить аналитику: ${result?.error || 'ошибка сервера'}`, 'err');
    setLoading(false);
  };

  useEffect(() => { loadReport(); }, [refreshKey, range.from, range.to]);

  const summary = report?.summary || {};
  const direction = report?.by_direction || [];
  const employees = report?.by_employee || [];
  const numbers = report?.by_number || [];
  const hours = report?.by_hour || [];
  const maxHour = useMemo(() => Math.max(1, ...hours.map((item) => Number(item.calls || 0))), [hours]);

  const selectPeriod = (kind) => {
    setPeriodKind(kind);
    setRange(periodRange(kind));
  };

  return (
    <div className="flex flex-col gap-6 animate-in fade-in duration-200">
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-xl font-bold text-white">Статистика и аналитика звонков</h1>
          <p className="text-xs text-muted mt-1">{report?.period ? `${report.period.from} — ${report.period.to}` : 'Выберите период'}</p>
        </div>
        <button onClick={() => openWithTicket('/api/v2/export/calls.csv')} className="px-4 py-2 rounded-xl bg-line/60 hover:bg-line font-semibold text-xs text-white flex items-center gap-1.5">
          <Download className="w-4 h-4" /> Экспорт звонков в CSV
        </button>
      </div>

      <div className="flex items-center gap-1.5 bg-[#081221] p-2 rounded-2xl border border-line w-fit">
        {[['today', 'Сегодня'], ['week', '7 дней'], ['month', '30 дней']].map(([kind, label]) => (
          <button key={kind} onClick={() => selectPeriod(kind)} className={`px-3 py-1.5 rounded-lg text-xs font-semibold ${periodKind === kind ? 'bg-acc text-white' : 'text-muted hover:text-white'}`}>{label}</button>
        ))}
      </div>

      {loading ? <div className="text-center py-12 text-muted">Загрузка аналитики...</div> : (
        <>
          <div className="grid grid-cols-2 lg:grid-cols-5 gap-3">
            {[
              ['Всего звонков', summary.total, 'text-white'],
              ['Успешных', summary.successful, 'text-ok'],
              ['Пропущенных', summary.missed, 'text-rose-400'],
              ['Средняя длительность', `${summary.average_duration_seconds || 0} с`, 'text-acc2'],
              ['Уникальных номеров', summary.unique_numbers, 'text-cy'],
            ].map(([label, value, color]) => <div key={label} className="glass-panel p-4 rounded-2xl"><span className="text-[11px] text-muted block mb-1">{label}</span><b className={`text-2xl font-bold ${color}`}>{typeof value === 'number' ? number(value) : value}</b></div>)}
          </div>

          <div className="flex gap-1 overflow-x-auto border-b border-line">
            {tabs.map(({ id, label, icon: Icon }) => <button key={id} onClick={() => setActiveTab(id)} className={`px-4 py-3 text-xs font-semibold whitespace-nowrap flex items-center gap-2 border-b-2 ${activeTab === id ? 'text-acc2 border-acc2' : 'text-muted border-transparent hover:text-white'}`}><Icon className="w-4 h-4" /> {label}</button>)}
          </div>

          <div className="glass-panel p-6 rounded-2xl overflow-x-auto">
            {activeTab === 'count' && <div className="grid lg:grid-cols-2 gap-8"><div><h2 className="text-base font-bold text-white mb-4">Звонки по дням</h2>{!(report?.by_day || []).length ? <Empty /> : <table className="w-full text-left text-xs"><thead><tr className="border-b border-line text-muted"><th className="py-2">Дата</th><th>Всего</th><th>Успешных</th><th>Минуты</th></tr></thead><tbody>{report.by_day.map((row) => <tr key={row.day} className="border-b border-line/40"><td className="py-2.5 text-white">{row.day}</td><td>{number(row.calls)}</td><td className="text-ok">{number(row.successful)}</td><td>{Math.round(Number(row.duration_seconds || 0) / 60)}</td></tr>)}</tbody></table>}</div><div><h2 className="text-base font-bold text-white mb-4">Результаты</h2>{!(report?.by_result || []).length ? <Empty /> : <table className="w-full text-left text-xs"><thead><tr className="border-b border-line text-muted"><th className="py-2">Результат</th><th>Звонков</th></tr></thead><tbody>{report.by_result.map((row) => <tr key={row.result || 'empty'} className="border-b border-line/40"><td className="py-2.5 text-white">{row.result || 'не указан'}</td><td>{number(row.calls)}</td></tr>)}</tbody></table>}</div></div>}

            {activeTab === 'direction' && <table className="w-full text-left text-xs"><thead><tr className="border-b border-line text-muted"><th className="py-2">Направление</th><th>Всего</th><th>Успешных</th><th>Пропущенных</th><th>Длительность</th></tr></thead><tbody>{direction.map((row) => <tr key={row.direction || 'unknown'} className="border-b border-line/40"><td className="py-3 text-white">{row.direction === 'in' ? 'Входящие' : row.direction === 'internal' ? 'Внутренние' : 'Исходящие'}</td><td>{number(row.calls)}</td><td className="text-ok">{number(row.successful)}</td><td className="text-rose-400">{number(row.missed)}</td><td>{Math.round(Number(row.duration_seconds || 0) / 60)} мин</td></tr>)}</tbody></table>}

            {activeTab === 'employees' && (employees.length ? <table className="w-full text-left text-xs"><thead><tr className="border-b border-line text-muted"><th className="py-2">Сотрудник</th><th>Добавочный</th><th>Всего</th><th>Успешных</th><th>Среднее</th></tr></thead><tbody>{employees.map((row) => <tr key={row.id} className="border-b border-line/40"><td className="py-3 text-white font-semibold">{row.name || 'Без имени'}</td><td>{row.extension || '—'}</td><td>{number(row.calls)}</td><td className="text-ok">{number(row.successful)}</td><td>{row.calls ? `${Math.round(Number(row.duration_seconds || 0) / row.calls)} с` : '0 с'}</td></tr>)}</tbody></table> : <Empty>Рейтинг появится после звонков через операторскую очередь</Empty>)}

            {activeTab === 'numbers' && (numbers.length ? <table className="w-full text-left text-xs"><thead><tr className="border-b border-line text-muted"><th className="py-2">Номер</th><th>Звонков</th><th>Успешных</th><th>Длительность</th></tr></thead><tbody>{numbers.map((row) => <tr key={row.number} className="border-b border-line/40"><td className="py-3 font-mono text-white">{row.number}</td><td>{number(row.calls)}</td><td className="text-ok">{number(row.successful)}</td><td>{Math.round(Number(row.duration_seconds || 0) / 60)} мин</td></tr>)}</tbody></table> : <Empty />)}

            {activeTab === 'hours' && (hours.length ? <div className="space-y-3"><p className="text-xs text-muted">Интенсивность звонков по дню недели и часу.</p>{hours.map((row) => <div key={`${row.weekday}-${row.hour}`} className="flex items-center gap-3 text-xs"><span className="w-16 text-muted">{['Вс', 'Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб'][row.weekday]} {String(row.hour).padStart(2, '0')}:00</span><div className="h-2 rounded-full bg-acc2/70" style={{ width: `${Math.max(2, (Number(row.calls) / maxHour) * 100)}%` }} /><span className="w-12 text-right text-white">{number(row.calls)}</span></div>)}</div> : <Empty />)}
          </div>
        </>
      )}
    </div>
  );
}
