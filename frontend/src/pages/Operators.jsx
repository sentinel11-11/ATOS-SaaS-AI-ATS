import React, { useState, useEffect } from 'react';
import { Users, PhoneIncoming, Clock } from 'lucide-react';
import { api } from '../api';
import Modal from '../components/Modal';

export default function Operators({ addToast, refreshKey, isAdmin }) {
  const [operators, setOperators] = useState([]);
  const [queue, setQueue] = useState([]);
  const [loading, setLoading] = useState(true);
  const [editingConnection, setEditingConnection] = useState(null);

  const saveConnection = async () => {
    const res = await api(`/operators/${editingConnection.id}/connection`, { body: {
      connection_method: editingConnection.connection_method || 'sip',
      mobile_phone: editingConnection.mobile_phone || '',
    } });
    if (res?.ok) {
      setEditingConnection(null);
      addToast('Способ подключения оператора сохранён', 'ok');
      loadAcdData();
    } else addToast(res?.detail || res?.error || 'Ошибка сохранения', 'err');
  };

  const loadAcdData = async () => {
    setLoading(true);
    const res = await api('/acd');

    if (res && res.operators) {
      setOperators(Array.isArray(res.operators) ? res.operators : []);
    }
    if (res && res.acd) {
      setQueue(Array.isArray(res.acd) ? res.acd : []);
    }
    setLoading(false);
  };

  useEffect(() => {
    loadAcdData();
  }, [refreshKey]);

  const operatorsList = Array.isArray(operators) ? operators : [];
  const queueList = Array.isArray(queue) ? queue : [];
  const setOperatorStatus = async (id, status) => {
    const res = await api(`/operators/${id}/status`, { method: 'POST', body: { status } });
    if (res?.ok) { addToast('Статус оператора изменён', 'ok'); loadAcdData(); }
    else addToast(res?.error || 'Не удалось изменить статус', 'err');
  };

  return (
    <div className="flex flex-col gap-6 animate-in fade-in duration-200">
      <h1 className="text-xl font-bold text-white">Операторы и ACD Очередь</h1>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Operators */}
        <div className="glass-panel p-6 rounded-2xl flex flex-col gap-4">
          <h2 className="text-base font-bold text-white flex items-center gap-2">
            <Users className="w-5 h-5 text-acc2" /> Операторы ({operatorsList.length})
          </h2>

          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead>
                <tr className="border-b border-line text-muted uppercase text-[10px]">
                  <th className="py-2.5 px-3">Оператор</th>
                  <th className="py-2.5 px-3">Внутр. №</th>
                  <th className="py-2.5 px-3">Статус</th>
                  <th className="py-2.5 px-3">Подключение</th>
                </tr>
              </thead>
              <tbody>
                {loading && operatorsList.length === 0 ? (
                  <tr>
                    <td colSpan={4} className="py-4 text-center text-muted">Загрузка...</td>
                  </tr>
                ) : operatorsList.length === 0 ? (
                  <tr>
                    <td colSpan={4} className="py-4 text-center text-dim">Операторов нет в ACD</td>
                  </tr>
                ) : (
                  operatorsList.map((op) => (
                    <tr key={op.id || op.login} className="border-b border-line/40">
                      <td className="py-2.5 px-3 font-semibold text-white">@{op.login} ({op.name || 'Оператор'})</td>
                      <td className="py-2.5 px-3 font-mono text-muted">{op.ext || '—'}</td>
                      <td className="py-2.5 px-3">
                        {isAdmin ? <select value={op.status} onChange={(e) => setOperatorStatus(op.id, e.target.value)} className="bg-[#0a1628] border border-line2 rounded-lg px-2 py-1 text-xs text-white">
                          <option value="free">Свободен</option><option value="break">Перерыв</option><option value="offline">Оффлайн</option><option value="busy">В разговоре</option>
                        </select> :
                        <span className={`px-2.5 py-0.5 rounded-full text-[11px] font-semibold ${
                          op.status === 'free' ? 'bg-ok/20 text-ok' : op.status === 'busy' ? 'bg-acc/20 text-acc2' : 'bg-warn/20 text-warn'
                        }`}>
                          {op.status === 'free' ? 'Свободен' : op.status === 'busy' ? 'В разговоре' : 'Перерыв'}
                        </span>}
                      </td>
                      <td className="py-2.5 px-3">
                        <span>{op.connection_method === 'mobile' ? 'Личный телефон' : 'SIP-устройство'}</span>
                        {op.mobile_phone && op.connection_method === 'mobile' && <span className="block font-mono text-muted">{op.mobile_phone}</span>}
                        {isAdmin && <button className="text-acc2 block mt-1" onClick={() => setEditingConnection({ ...op })}>
                          Настроить подключение
                        </button>}
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </div>

        {/* ACD Queue */}
        <div className="glass-panel p-6 rounded-2xl flex flex-col gap-4">
          <h2 className="text-base font-bold text-white flex items-center gap-2">
            <PhoneIncoming className="w-5 h-5 text-cy" /> Очередь ожидающих звонков ({queueList.length})
          </h2>

          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead>
                <tr className="border-b border-line text-muted uppercase text-[10px]">
                  <th className="py-2.5 px-3"># ID</th>
                  <th className="py-2.5 px-3">Телефон</th>
                  <th className="py-2.5 px-3">Ожидание</th>
                </tr>
              </thead>
              <tbody>
                {queueList.length === 0 ? (
                  <tr>
                    <td colSpan={3} className="py-6 text-center text-dim">Очередь вызовов пуста.</td>
                  </tr>
                ) : (
                  queueList.map((item) => (
                    <tr key={item.id} className="border-b border-line/40">
                      <td className="py-2.5 px-3 font-mono text-dim">#{item.id}</td>
                      <td className="py-2.5 px-3 font-mono font-semibold text-white">{item.phone || item.contact_phone || '—'}</td>
                      <td className="py-2.5 px-3 text-muted">{item.wait_sec ? `${item.wait_sec} с` : '0 с'}</td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </div>
      </div>
      <Modal isOpen={!!editingConnection} onClose={() => setEditingConnection(null)} title="Подключение оператора">
        {editingConnection && <div className="flex flex-col gap-4 text-sm">
          <label className="text-muted">Способ разговора
            <select className="block w-full bg-[#0a1628] border border-line2 rounded-xl p-2 text-white" value={editingConnection.connection_method || 'sip'} onChange={(e) => setEditingConnection({ ...editingConnection, connection_method: e.target.value })}>
              <option value="sip">SIP-приложение / SIP-телефон</option>
              <option value="mobile">Личный телефон — сначала звонок менеджеру</option>
            </select>
          </label>
          {editingConnection.connection_method === 'mobile' && <>
            <label className="text-muted">Личный номер менеджера
              <input className="block w-full bg-[#0a1628] border border-line2 rounded-xl p-2 text-white" value={editingConnection.mobile_phone || ''} placeholder="+79990000000" onChange={(e) => setEditingConnection({ ...editingConnection, mobile_phone: e.target.value })} />
            </label>
            <p className="text-muted">После ответа нажмите 1. Затем ATOS проверит разрешение и наберёт клиента. Для разговора нужны два внешних канала. В тестовой среде оба номера должны быть в списке разрешённых направлений.</p>
          </>}
          <button className="bg-acc text-white rounded-xl py-2" onClick={saveConnection}>Сохранить подключение</button>
        </div>}
      </Modal>
    </div>
  );
}
