export type Me = { tenant_id: string; login: string; role: string };
export type Contact = {
  id: string;
  name: string;
  phone: string;
  consent: boolean;
  blocked: boolean;
  consent_source: string;
};
export type Campaign = {
  id: string;
  name: string;
  route: string;
  status: string;
  max_channels: number;
};
export type Attempt = {
  id: string;
  route: string;
  status: string;
  reason: string;
  created_at: string;
};
export const labels: Record<string, string> = {
  draft: "Черновик",
  running: "Запущена",
  paused: "Пауза",
  completed: "Завершено",
  failed: "Ошибка",
  unknown: "Требует сверки",
  prepared: "В очереди",
  dispatching: "Отправляется",
  accepted: "Принят оператором",
  answered: "Ответ",
  ringing: "Входящий вызов",
  queued: "В очереди",
  blocked: "Заблокирован",
  cancelled: "Отменён",
  sim: "Симулятор",
  multicom_ami: "Мультиком · Asterisk",
  megafon_vats: "МегаФон ВАТС",
};

export type Actions = {
  admin: boolean;
  busy: boolean;
  action: (fn: () => Promise<void>) => Promise<void>;
  refresh: () => Promise<void>;
  setNotice: (message: string) => void;
};
