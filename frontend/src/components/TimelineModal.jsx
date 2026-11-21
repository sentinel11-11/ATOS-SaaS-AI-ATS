import React, { useState, useEffect } from 'react';
import { Clock, Phone, Radio, Shield } from 'lucide-react';
import { api } from '../api';
import Modal from './Modal';

const REASON_MESSAGES = {
  ami_endpoint_unavailable: 'Asterisk отклонил проверку PJSIP endpoint выбранного А-номера. Проверьте alias mcm-cli и право AMI system.',
  ami_permission_denied: 'Asterisk отказал AMI-аккаунту в выполнении действия. Проверьте права выделенного аккаунта ATOS.',
  ami_authentication_failed: 'Asterisk отклонил вход AMI-аккаунта. Проверьте пользователя и сохранённый пароль AMI.',
  ami_action_rejected: 'Asterisk отклонил действие AMI. Проверьте настройки аккаунта и журнал Asterisk для этой попытки.',
  media_directory_missing: 'Подготовьте настроенный каталог медиарежима и права доступа пользователя ats-beta.',
  enable_media_before_dialing_template: 'Для выбранного шаблона требуется подготовленный медиарежим Asterisk.',
  playback_failed: 'Asterisk сообщил об ошибке воспроизведения сообщения.',
  media_failed: 'Asterisk сообщил об ошибке выполнения медиасценария AGI.',
  ami_hangup_fallback: 'Asterisk сообщил о завершении канала. Подробности уточняются по коду завершения и диалплану.',
  ami_terminal_event_missing: 'Получено завершение канала без итогового результата сценария. Сверьте вызов с CDR/CEL и подтвердите результат перед новой попыткой.',
  callback_bridge_unconfirmed: 'Итог сценария получен, но соединение двух плеч не подтверждено. Требуется сверка Asterisk/CDR/CEL.',
  callback_evidence_missing: 'Истекло ожидание событий двух плеч. Каналы остаются зарезервированы до сверки Asterisk/CDR/CEL.',
  callback_leg_tracking_missing: 'Не получены данные каналов телефонного соединения. Требуется сверка Asterisk/CDR/CEL.',
  operator_not_confirmed: 'Подтверждение клавишей 1 не получено. Клиент не набирался.',
  callback_prompt_unavailable: 'Asterisk не нашёл файл сигнала подтверждения. Повторно выполните установку callback на VPS. Клиент не набирался.',
  callback_confirmation_failed: 'Asterisk сообщил об ошибке ожидания подтверждения. Проверьте файл сигнала и журнал Read на VPS. Клиент не набирался.',
  callback_parameters_invalid: 'Параметры callback отклонены диалпланом Asterisk. Клиент не набирался.',
  callback_gate_denied: 'ATOS отклонил набор клиента после повторной проверки кампании, контакта и оператора.',
  callback_authorization_timeout: 'Истекло ожидание разрешения ATOS на набор клиента.',
  operator_busy: 'Телефон менеджера занят.',
  operator_no_answer: 'Менеджер не ответил.',
  customer_busy: 'Телефон клиента занят.',
  customer_no_answer: 'Клиент не ответил.',
};

export default function TimelineModal({ callId, onClose }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!callId) return;
    setLoading(true);
    async function load() {
      const res = await api(`/calls/${callId}/timeline`);
      if (res && res.call) {
        setData(res);
      }
      setLoading(false);
    }
    load();
  }, [callId]);

  if (!callId) return null;

  const { call = {}, events = [], acd = [] } = data || {};
  const eventsList = Array.isArray(events) ? events : [];
  const failureReason = call.detail || call.reason || '';
  const failed = ['failed', 'unknown'].includes(call.status);

  return (
    <Modal isOpen={!!callId} onClose={onClose} title={`Таймлайн и Хронология ВАТС: Звонок #${callId}`} wide={true}>
      {loading ? (
        <div className="py-8 text-center text-muted animate-pulse">Загрузка хронологии ВАТС...</div>
      ) : !data ? (
        <div className="py-8 text-center text-bad">Не удалось загрузить данные звонка</div>
      ) : (
        <div className="flex flex-col gap-4 text-xs">
          {/* Call Summary */}
          <div className="p-4 rounded-xl bg-[#081221] border border-line grid grid-cols-2 md:grid-cols-4 gap-3">
            <div>
              <span className="text-dim block">Клиент</span>
              <span className="font-bold text-white">{call.contact_name || call.contact_phone}</span>
            </div>
            <div>
              <span className="text-dim block">Исходящий номер</span>
              <span className="font-mono text-white">{call.caller_id || '—'}</span>
            </div>
            <div>
              <span className="text-dim block">Результат</span>
              <span className="font-semibold text-acc2">{call.result || call.status}</span>
            </div>
            <div>
              <span className="text-dim block">Длительность</span>
              <span className="font-semibold text-white">{call.duration_sec ? `${Math.round(call.duration_sec)} с` : '—'}</span>
            </div>
          </div>

          {failureReason && (
            <div className={`p-4 rounded-xl bg-[#081221] border ${failed ? 'border-bad/40' : 'border-line'}`} role={failed ? 'alert' : 'status'}>
              <span className="text-dim block mb-1">Причина завершения</span>
              <code className="font-mono text-white break-all">{failureReason}</code>
              {REASON_MESSAGES[failureReason] && (
                <p className="text-white mt-2">{REASON_MESSAGES[failureReason]}</p>
              )}
            </div>
          )}

          {Array.isArray(data.legs) && data.legs.length > 0 && <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            {data.legs.map((leg) => <div key={leg.role} className="p-4 rounded-xl bg-[#081221] border border-line">
              <b className="block text-white">{leg.role === 'operator' ? 'Плечо менеджера' : 'Плечо клиента'}</b>
              <span className="font-mono text-muted block">{leg.phone || '—'}</span>
              <span className="text-white block">{leg.status}{leg.dial_status ? ` · ${leg.dial_status}` : ''}</span>
              {leg.answered_at && <span className="text-muted block">Ответ сети: {leg.answered_at.replace('T', ' ').slice(0, 19)}</span>}
              {leg.bridged_at && <span className="text-muted block">В мосту: {leg.bridged_at.replace('T', ' ').slice(0, 19)}</span>}
            </div>)}
          </div>}

          {/* Timeline Events List */}
          <div className="flex flex-col gap-2">
            <span className="font-bold text-white text-sm mb-1">События ВАТС и Вектор вебхуков ({eventsList.length})</span>
            {eventsList.length === 0 ? (
              <div className="p-4 rounded-xl bg-[#081221] text-dim text-center">
                Событий ВАТС по этому звонку не зафиксировано
              </div>
            ) : (
              eventsList.map((ev, idx) => (
                <div key={idx} className="p-3 rounded-xl bg-[#081221] border border-line flex items-center justify-between">
                  <div className="flex items-center gap-3">
                    <span className="p-1.5 rounded-lg bg-acc/20 text-acc2">
                      <Radio className="w-4 h-4" />
                    </span>
                    <div>
                      <span className="font-bold text-white block">{ev.event_type === 'channel_hangup' ? 'Завершение канала Asterisk' : ev.event_type || 'event'}</span>
                      {ev.detail && <span className="text-white block break-all">{ev.detail}</span>}
                      {Number.isInteger(ev.hangup_cause) && (
                        <span className="text-white block">Код завершения Asterisk {ev.hangup_cause}</span>
                      )}
                      {Number.isInteger(ev.originate_reason) && (
                        <span className="text-white block">Код ответа Originate {ev.originate_reason}</span>
                      )}
                      {ev.dial_status && <span className="text-white block">Результат Dial {ev.dial_status}</span>}
                      <span className="text-[11px] text-dim">
                        Fingerprint: {ev.fingerprint?.slice(0, 16) || '—'}...
                      </span>
                    </div>
                  </div>
                  <span className="font-mono text-dim">{ev.received_at?.replace('T', ' ').slice(0, 19)}</span>
                </div>
              ))
            )}
          </div>
        </div>
      )}
    </Modal>
  );
}
