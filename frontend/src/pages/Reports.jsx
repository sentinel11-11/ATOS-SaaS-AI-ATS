import React, { useEffect, useMemo, useState } from 'react';
import {
  ArrowDownRight,
  ArrowUpRight,
  BarChart3,
  CheckCircle2,
  Clock3,
  Download,
  Filter,
  Hash,
  PhoneCall,
  RefreshCw,
  Users,
} from 'lucide-react';
import { api, openWithTicket } from '../api';

const DAY_NAMES = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'];
const DIRECTION_NAMES = { in: 'Входящие', out: 'Исходящие', internal: 'Внутренние' };
const RESULT_NAMES = {
  completed: 'Успешно', done_ok: 'Успешно', operator_ok: 'Успешно', done_agent: 'Успешно', answered: 'Отвечен',
  missed: 'Пропущен', failed: 'Неуспешный', busy: 'Занято', no_answer: 'Нет ответа',
  canceled: 'Отменён', cancelled: 'Отменён', not_available: 'Недоступен',
};
const TABS = [
  { id: 'count', label: 'Количество звонков', icon: BarChart3 },
  { id: 'direction', label: 'Направление звонков', icon: PhoneCall },
  { id: 'employees', label: 'Рейтинг сотрудников', icon: Users },
  { id: 'numbers', label: 'Статистика по номерам', icon: Hash },
  { id: 'hours', label: 'Нагрузка по часам', icon: Clock3 },
];

const asNumber = (value) => Number(value || 0);
const number = (value, digits = 0) => asNumber(value).toLocaleString('ru-RU', {
  minimumFractionDigits: digits,
  maximumFractionDigits: digits,
});
const duration = (seconds) => {
  const total = Math.max(0, Math.round(asNumber(seconds)));
  if (total < 60) return `${total} с`;
  const minutes = Math.floor(total / 60);
  const rest = total % 60;
  return rest ? `${minutes} мин ${rest} с` : `${minutes} мин`;
};
const resultName = (value) => RESULT_NAMES[value] || (value ? value.replaceAll('_', ' ') : 'Не указан');
const dateToInput = (value) => {
  const date = new Date(value);
  const offset = date.getTimezoneOffset() * 60000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 10);
};

function rangeFor(kind) {
  const today = new Date();
  const to = dateToInput(today);
  const from = new Date(today);
  if (kind === 'week') from.setDate(today.getDate() - 6);
  if (kind === 'month') from.setDate(today.getDate() - 29);
  if (kind === 'year') from.setDate(today.getDate() - 364);
  return { from: dateToInput(from), to };
}

function Empty({ children = 'Нет данных за выбранный период' }) {
  return <div className="py-14 text-center text-dim text-sm">{children}</div>;
}

function Kpi({ label, value, hint, color = 'text-white', delta }) {
  const positive = asNumber(delta) >= 0;
  return (
    <div className="glass-panel rounded-2xl p-4 min-h-[104px] flex flex-col justify-between">
      <span className="text-[11px] uppercase tracking-wide text-muted">{label}</span>
      <div className="flex items-end justify-between gap-2">
        <strong className={`text-2xl leading-none font-bold ${color}`}>{value}</strong>
        {delta !== undefined && (
          <span className={`text-[11px] flex items-center gap-0.5 ${positive ? 'text-ok' : 'text-bad'}`}>
            {positive ? <ArrowUpRight className="w-3.5 h-3.5" /> : <ArrowDownRight className="w-3.5 h-3.5" />}
            {Math.abs(asNumber(delta)).toLocaleString('ru-RU')}%
          </span>
        )}
      </div>
      {hint && <span className="text-[11px] text-dim">{hint}</span>}
    </div>
  );
}

function SectionTitle({ children, action }) {
  return (
    <div className="flex items-center justify-between gap-3 mb-4">
      <h2 className="text-base font-bold text-white">{children}</h2>
      {action}
    </div>
  );
}

function DirectionPill({ direction }) {
  const color = direction === 'in' ? 'text-cy bg-cy/10' : direction === 'internal' ? 'text-warn bg-warn/10' : 'text-acc2 bg-acc2/10';
  return <span className={`inline-flex px-2 py-1 rounded-md text-[11px] font-semibold ${color}`}>{DIRECTION_NAMES[direction] || direction}</span>;
}

function AnalyticsFilters({ range, setRange, periodKind, setPeriodKind, draftFilters, setDraftFilters, onApply, loading }) {
  const choosePeriod = (kind) => {
    setPeriodKind(kind);
    if (kind !== 'custom') setRange(rangeFor(kind));
  };
  return (
    <div className="glass-panel rounded-2xl p-3 flex flex-col gap-3">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-1.5 flex-wrap">
          {[['today', 'Сегодня'], ['week', '7 дней'], ['month', '30 дней'], ['year', '12 месяцев'], ['custom', 'Период']].map(([kind, label]) => (
            <button key={kind} onClick={() => choosePeriod(kind)} className={`px-3 py-1.5 rounded-lg text-xs font-semibold transition-colors ${periodKind === kind ? 'bg-acc text-white' : 'text-muted hover:text-white hover:bg-line/60'}`}>
              {label}
            </button>
          ))}
        </div>
        <span className="text-[11px] text-dim flex items-center gap-1.5"><Filter className="w-3.5 h-3.5" /> Все данные из журнала звонков</span>
      </div>
      <div className="flex items-end gap-2 flex-wrap">
        <label className="text-[11px] text-muted flex flex-col gap-1">
          От
          <input type="date" value={range.from} onChange={(event) => { setPeriodKind('custom'); setRange((current) => ({ ...current, from: event.target.value })); }} className="bg-[#0b1728] border border-line rounded-lg px-2.5 py-2 text-xs text-white" />
        </label>
        <label className="text-[11px] text-muted flex flex-col gap-1">
          До
          <input type="date" value={range.to} onChange={(event) => { setPeriodKind('custom'); setRange((current) => ({ ...current, to: event.target.value })); }} className="bg-[#0b1728] border border-line rounded-lg px-2.5 py-2 text-xs text-white" />
        </label>
        <select value={draftFilters.direction} onChange={(event) => setDraftFilters((current) => ({ ...current, direction: event.target.value }))} className="bg-[#0b1728] border border-line rounded-lg px-2.5 py-2 text-xs text-white">
          <option value="">Все направления</option><option value="in">Входящие</option><option value="out">Исходящие</option><option value="internal">Внутренние</option>
        </select>
        <input value={draftFilters.number} onChange={(event) => setDraftFilters((current) => ({ ...current, number: event.target.value }))} onKeyDown={(event) => { if (event.key === 'Enter') onApply(); }} placeholder="Номер абонента" className="bg-[#0b1728] border border-line rounded-lg px-2.5 py-2 text-xs text-white placeholder:text-dim" />
        <button onClick={onApply} className="px-3 py-2 rounded-lg bg-acc hover:bg-acc2 text-white text-xs font-semibold flex items-center gap-1.5">
          {loading ? <RefreshCw className="w-3.5 h-3.5 animate-spin" /> : <Filter className="w-3.5 h-3.5" />} Применить
        </button>
      </div>
    </div>
  );
}

function CountReport({ report }) {
  const days = report.by_day || [];
  const months = report.by_month || [];
  const results = report.by_result || [];
  const maxDay = Math.max(1, ...days.map((item) => asNumber(item.calls)));
  const maxResult = Math.max(1, ...results.map((item) => asNumber(item.calls)));
  return (
    <div className="grid xl:grid-cols-[1.4fr_1fr] gap-6">
      <div>
        <SectionTitle>Сводка по месяцам</SectionTitle>
        {!months.length ? <Empty /> : <div className="overflow-x-auto"><table className="w-full text-left text-xs"><thead><tr className="border-b border-line text-muted"><th className="py-2 pr-3">Период</th><th>Минуты</th><th>Звонки</th><th>Успешные</th><th>Пропущенные</th><th>Входящие</th><th>Исходящие</th></tr></thead><tbody>{months.map((row) => <tr key={row.month} className="border-b border-line/40"><td className="py-3 pr-3 text-white font-semibold">{row.month}</td><td>{number(row.minutes, 1)}</td><td>{number(row.calls)}</td><td className="text-ok">{number(row.successful)}</td><td className="text-bad">{number(row.missed)}</td><td className="text-cy">{number(row.incoming)}</td><td className="text-acc2">{number(row.outgoing)}</td></tr>)}</tbody></table></div>}
        <SectionTitle action={<span className="text-[11px] text-dim">{days.length} дней</span>}>Динамика по дням</SectionTitle>
        {!days.length ? <Empty /> : <div className="space-y-2">{days.map((row) => <div key={row.day} className="grid grid-cols-[58px_1fr_48px] items-center gap-2 text-xs"><span className="text-muted">{row.day.slice(5)}</span><div className="h-2 rounded-full bg-[#10213a] overflow-hidden"><div className="h-full rounded-full bg-acc2" style={{ width: `${Math.max(row.calls ? 3 : 0, (asNumber(row.calls) / maxDay) * 100)}%` }} /></div><span className="text-right text-white">{number(row.calls)}</span></div>)}</div>}
      </div>
      <div>
        <SectionTitle>Результаты звонков</SectionTitle>
        {!results.length ? <Empty /> : <div className="space-y-3">{results.map((row) => <div key={row.result || 'unknown'}><div className="flex justify-between text-xs mb-1.5"><span className="text-white">{resultName(row.result)}</span><span className="text-muted">{number(row.calls)}</span></div><div className="h-2 bg-[#10213a] rounded-full overflow-hidden"><div className="h-full bg-ok rounded-full" style={{ width: `${(asNumber(row.calls) / maxResult) * 100}%` }} /></div></div>)}</div>}
        <div className="mt-8 rounded-xl bg-[#0b1728] border border-line/70 p-4"><span className="text-xs text-muted">Длительность разговоров</span><div className="text-xl text-white font-bold mt-1">{duration(report.summary?.duration_seconds)}</div><div className="text-[11px] text-dim mt-1">Средняя: {duration(report.summary?.average_duration_seconds)}</div></div>
      </div>
    </div>
  );
}

function DirectionReport({ report }) {
  const rows = report.by_direction || [];
  const monthly = report.by_direction_month || [];
  return (
    <div className="space-y-6">
      <div className="grid md:grid-cols-3 gap-3">{rows.map((row) => <div key={row.direction} className="rounded-2xl bg-[#0b1728] border border-line/70 p-4"><DirectionPill direction={row.direction} /><div className="text-2xl font-bold text-white mt-3">{number(row.calls)}</div><div className="text-xs text-muted mt-1">{number(row.successful)} успешных · {number(row.missed)} пропущенных</div><div className="text-[11px] text-dim mt-2">{duration(row.duration_seconds)} разговоров</div></div>)}</div>
      <div className="overflow-x-auto"><SectionTitle>Сравнение направлений по месяцам</SectionTitle>{!monthly.length ? <Empty /> : <table className="w-full text-left text-xs"><thead><tr className="border-b border-line text-muted"><th className="py-2">Направление</th><th>Месяц</th><th>Звонки</th><th>Минуты</th></tr></thead><tbody>{monthly.map((row, index) => <tr key={`${row.direction}-${row.month}-${index}`} className="border-b border-line/40"><td className="py-3"><DirectionPill direction={row.direction} /></td><td>{String(row.month).slice(0, 7)}</td><td className="text-white">{number(row.calls)}</td><td>{number(asNumber(row.duration_seconds) / 60, 1)}</td></tr>)}</tbody></table>}</div>
    </div>
  );
}

function EmployeeReport({ report }) {
  const employees = report.by_employee || [];
  const average = report.employee_average || {};
  return (
    <div>
      <div className="rounded-xl bg-[#0b1728] border border-line/70 p-4 mb-4"><div className="text-xs text-muted">Средний сотрудник</div><div className="grid grid-cols-2 md:grid-cols-6 gap-3 mt-3 text-xs"><div><b className="text-white text-lg">{number(average.calls)}</b><span className="block text-dim">успешных звонков</span></div><div><b className="text-white text-lg">{number(average.incoming_successful)}</b><span className="block text-dim">входящих</span></div><div><b className="text-white text-lg">{number(average.outgoing_successful)}</b><span className="block text-dim">исходящих</span></div><div><b className="text-white text-lg">{number(average.calls_per_day, 1)}</b><span className="block text-dim">звонков в день</span></div><div><b className="text-white text-lg">{number(average.calls_per_hour, 2)}</b><span className="block text-dim">в час (24 ч)</span></div><div><b className="text-white text-lg">{number(average.load_percent, 1)}%</b><span className="block text-dim">загрузка</span></div></div></div>
      {!employees.length ? <Empty>Рейтинг появится после звонков, назначенных сотрудникам</Empty> : <div className="overflow-x-auto"><table className="w-full text-left text-xs"><thead><tr className="border-b border-line text-muted"><th className="py-2 w-10">№</th><th>Сотрудник</th><th>Всего успешных</th><th>Входящих</th><th>Исходящих</th><th>Звонков в день</th><th>В час (24 ч)</th><th>Загрузка</th></tr></thead><tbody>{employees.map((row) => <tr key={row.id} className={`border-b border-line/40 ${row.above_average ? 'bg-acc/5' : ''}`}><td className="py-3 text-dim">{row.rank}</td><td><div className="text-white font-semibold">{row.name}</div><div className="text-[11px] text-dim">{row.extension}</div></td><td className="text-white font-semibold">{number(row.total_successful)}</td><td>{number(row.incoming_successful)}</td><td>{number(row.outgoing_successful)}</td><td>{number(row.calls_per_day, 1)}</td><td>{number(row.calls_per_hour, 2)}</td><td><div className="flex items-center gap-2"><div className="w-20 h-1.5 bg-[#10213a] rounded-full overflow-hidden"><div className="h-full bg-acc2 rounded-full" style={{ width: `${Math.min(100, asNumber(row.load_percent))}%` }} /></div><span>{number(row.load_percent, 1)}%</span></div></td></tr>)}</tbody></table></div>}
    </div>
  );
}

function NumberReport({ report, selectedNumbers, setSelectedNumbers }) {
  const rows = report.by_number || [];
  const allSelected = rows.length > 0 && rows.every((row) => selectedNumbers.has(row.number));
  const toggle = (value) => setSelectedNumbers((current) => { const next = new Set(current); if (next.has(value)) next.delete(value); else next.add(value); return next; });
  return (
    <div className="overflow-x-auto"><SectionTitle action={selectedNumbers.size ? <span className="text-[11px] text-acc2">Выбрано: {selectedNumbers.size}</span> : null}>Статистика по номерам</SectionTitle>{!rows.length ? <Empty /> : <table className="w-full text-left text-xs"><thead><tr className="border-b border-line text-muted"><th className="py-2 w-10"><input type="checkbox" checked={allSelected} onChange={() => setSelectedNumbers(allSelected ? new Set() : new Set(rows.map((row) => row.number)))} /></th><th>Номер</th><th>Входящие</th><th>Пропущенные</th><th>Уникальных входящих</th><th>Уникальных пропущенных</th><th>Успешных</th><th>Минуты</th></tr></thead><tbody>{rows.map((row) => <tr key={row.number} className="border-b border-line/40"><td className="py-3"><input type="checkbox" checked={selectedNumbers.has(row.number)} onChange={() => toggle(row.number)} /></td><td className="font-mono text-white font-semibold">{row.number === '—' ? 'Не определён' : row.number}</td><td className="text-cy">{number(row.incoming)}</td><td className="text-bad">{number(row.missed)}</td><td>{number(row.unique_incoming)}</td><td>{number(row.unique_missed)}</td><td className="text-ok">{number(row.successful)}</td><td>{number(asNumber(row.duration_seconds) / 60, 1)}</td></tr>)}</tbody></table>}</div>
  );
}

function HourReport({ report }) {
  const cells = report.by_hour || [];
  const max = asNumber(report.hour_summary?.max);
  const cell = (day, hour) => cells.find((item) => item.weekday === day && item.hour === hour) || { calls: 0 };
  const color = (calls) => {
    if (!calls || !max) return 'rgba(122,163,220,.06)';
    return `rgba(61,134,255,${Math.max(0.18, Math.min(0.95, calls / max))})`;
  };
  return (
    <div><SectionTitle action={<div className="flex items-center gap-3 text-[11px] text-dim"><span className="flex items-center gap-1"><i className="w-2.5 h-2.5 rounded-sm bg-[#10213a]" /> Нет звонков</span><span className="flex items-center gap-1"><i className="w-2.5 h-2.5 rounded-sm bg-acc" /> {number(report.hour_summary?.min_positive)} звонок</span><span className="flex items-center gap-1"><i className="w-2.5 h-2.5 rounded-sm bg-acc2" /> {number(max)} звонков</span></div>}>Нагрузка по часам</SectionTitle><div className="overflow-x-auto pb-2"><div className="min-w-[960px]"><div className="grid grid-cols-[48px_repeat(24,minmax(30px,1fr))] gap-1 text-[10px] text-dim mb-1"><span />{Array.from({ length: 24 }, (_, hour) => <span key={hour} className="text-center">{String(hour).padStart(2, '0')}</span>)}</div>{DAY_NAMES.map((day, dayIndex) => <div key={day} className="grid grid-cols-[48px_repeat(24,minmax(30px,1fr))] gap-1 mb-1"><span className="text-[11px] text-muted self-center">{day}</span>{Array.from({ length: 24 }, (_, hour) => { const item = cell(dayIndex, hour); return <div key={hour} title={`${day} ${String(hour).padStart(2, '0')}:00 — ${number(item.calls)} звонков`} className="h-8 rounded-sm flex items-center justify-center text-[10px] text-white/90" style={{ backgroundColor: color(asNumber(item.calls)) }}>{item.calls ? number(item.calls) : ''}</div>; })}</div>)}</div></div><div className="grid md:grid-cols-2 gap-3 mt-5"><div className="rounded-xl bg-[#0b1728] border border-line/70 p-3 text-xs text-muted">Пиковая нагрузка: <strong className="text-white">{report.hour_summary?.peak ? `${DAY_NAMES[report.hour_summary.peak.weekday]}, ${String(report.hour_summary.peak.hour).padStart(2, '0')}:00` : 'нет данных'}</strong></div><div className="rounded-xl bg-[#0b1728] border border-line/70 p-3 text-xs text-muted">Максимум за час: <strong className="text-white">{number(max)} звонков</strong></div></div></div>
  );
}

export default function Reports({ addToast, refreshKey }) {
  const [range, setRange] = useState(() => rangeFor('month'));
  const [periodKind, setPeriodKind] = useState('month');
  const [activeTab, setActiveTab] = useState('count');
  const [draftFilters, setDraftFilters] = useState({ direction: '', number: '' });
  const [filters, setFilters] = useState({ direction: '', number: '' });
  const [report, setReport] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [selectedNumbers, setSelectedNumbers] = useState(new Set());

  useEffect(() => {
    let active = true;
    const load = async () => {
      setLoading(true);
      setError('');
      const params = new URLSearchParams({ from: range.from, to: range.to });
      if (filters.direction) params.set('direction', filters.direction);
      if (filters.number) params.set('number', filters.number);
      const result = await api(`/analytics?${params.toString()}`);
      if (!active) return;
      if (result?.ok) setReport(result);
      else { setError(result?.error || 'Не удалось загрузить аналитику'); addToast?.(`Не удалось загрузить аналитику: ${result?.error || 'ошибка сервера'}`, 'err'); }
      setLoading(false);
    };
    load();
    return () => { active = false; };
  }, [addToast, filters.direction, filters.number, range.from, range.to, refreshKey]);

  const exportUrl = useMemo(() => {
    const params = new URLSearchParams({ from: range.from, to: range.to });
    if (filters.direction) params.set('direction', filters.direction);
    if (filters.number) params.set('number', filters.number);
    return `/api/v2/export/analytics.csv?${params.toString()}`;
  }, [filters.direction, filters.number, range.from, range.to]);
  const summary = report?.summary || {};
  const delta = report?.previous?.delta_percent || {};
  const periodText = report?.period ? `${report.period.from} — ${report.period.to}` : `${range.from} — ${range.to}`;

  return (
    <div className="flex flex-col gap-5 animate-in fade-in duration-200">
      <div className="flex items-start justify-between gap-3 flex-wrap"><div><h1 className="text-xl font-bold text-white">Статистика и аналитика звонков</h1><p className="text-xs text-muted mt-1">Период: {periodText} · {report?.period?.timezone || 'часовой пояс организации'}</p></div><button onClick={() => openWithTicket(exportUrl)} className="px-3.5 py-2 rounded-xl bg-line/60 hover:bg-line font-semibold text-xs text-white flex items-center gap-1.5"><Download className="w-4 h-4" /> Экспорт аналитики CSV</button></div>
      <AnalyticsFilters range={range} setRange={setRange} periodKind={periodKind} setPeriodKind={setPeriodKind} draftFilters={draftFilters} setDraftFilters={setDraftFilters} onApply={() => setFilters(draftFilters)} loading={loading} />
      {error && <div className="rounded-xl border border-bad/30 bg-bad/10 text-bad text-xs px-4 py-3">{error}</div>}
      {loading && !report ? <div className="text-center py-16 text-muted"><RefreshCw className="w-5 h-5 animate-spin mx-auto mb-3" />Загрузка аналитики...</div> : report && <>
        <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-6 gap-3"><Kpi label="Всего звонков" value={number(summary.total)} delta={delta.total} /><Kpi label="Успешных" value={number(summary.successful)} hint={`${number(summary.answer_rate, 1)}% дозвона`} color="text-ok" delta={delta.successful} /><Kpi label="Пропущенных" value={number(summary.missed)} color="text-bad" delta={delta.missed} /><Kpi label="Входящих" value={number(summary.incoming)} color="text-cy" /><Kpi label="Исходящих" value={number(summary.outgoing)} color="text-acc2" /><Kpi label="Уникальных номеров" value={number(summary.unique_numbers)} color="text-warn" delta={delta.unique_numbers} /></div>
        <div className="flex gap-1 overflow-x-auto border-b border-line">{TABS.map(({ id, label, icon: Icon }) => <button key={id} onClick={() => setActiveTab(id)} className={`px-4 py-3 text-xs font-semibold whitespace-nowrap flex items-center gap-2 border-b-2 transition-colors ${activeTab === id ? 'text-acc2 border-acc2' : 'text-muted border-transparent hover:text-white'}`}><Icon className="w-4 h-4" /> {label}</button>)}</div>
        <div className="glass-panel p-5 md:p-6 rounded-2xl">{activeTab === 'count' && <CountReport report={report} />}{activeTab === 'direction' && <DirectionReport report={report} />}{activeTab === 'employees' && <EmployeeReport report={report} />}{activeTab === 'numbers' && <NumberReport report={report} selectedNumbers={selectedNumbers} setSelectedNumbers={setSelectedNumbers} />}{activeTab === 'hours' && <HourReport report={report} />}</div>
        <div className="flex items-center gap-2 text-[11px] text-dim"><CheckCircle2 className="w-3.5 h-3.5 text-ok" /> Данные из текущего журнала звонков. Пересчёт учитывает часовой пояс организации и сравнивает период с предыдущим аналогичным периодом.</div>
      </>}
    </div>
  );
}
