import React, { useState } from 'react';
import { Phone, Lock, User, ArrowRight } from 'lucide-react';
import { api, setSession } from '../api';

export default function AuthGate({ onLoginSuccess }) {
  const [login, setLogin] = useState('admin');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [tenantId, setTenantId] = useState(() => new URLSearchParams(window.location.search).get('workspace') || '');
  const [needsWorkspace, setNeedsWorkspace] = useState(false);

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (!login || !password) return setError('Заполните все поля');
    setError('');
    setLoading(true);

    const res = await api('/auth/login', {
      body: { login, password, ...(tenantId ? { tenant_id: tenantId.trim() } : {}) },
    });

    setLoading(false);
    if (res && res.ok) {
      const user = { id: res.user_id, login: res.login, role: res.role, tenant_id: res.tenant_id };
      setSession(true, user);
      if (onLoginSuccess) onLoginSuccess(user);
    } else {
      if (res.error === 'tenant_required' || res.error === 'workspace_required') {
        setNeedsWorkspace(true);
        setError('Укажите ID рабочего пространства');
        return;
      }
      setError(
        ['rate_limit', 'too_many_attempts', 'login_rate_limited'].includes(res.error)
          ? `Слишком много попыток входа${res.retry_after_sec ? `, повторите через ${res.retry_after_sec} с` : ''}`
          : 'Неверный логин или пароль'
      );
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-[#070d17] bg-[radial-gradient(ellipse_at_top,_var(--tw-gradient-stops))] from-acc/10 via-bg0 to-bg0">
      <div className="w-full max-w-md bg-gradient-to-b from-[#122139]/90 to-[#09111e]/95 border border-line rounded-3xl p-8 shadow-2xl backdrop-blur-xl">
        {/* Brand */}
        <div className="flex items-center gap-3 mb-6">
          <div className="w-11 h-11 rounded-2xl bg-gradient-to-br from-[#5b8cff] to-[#3d6bff] flex items-center justify-center text-white shadow-lg shadow-acc/30">
            <Phone className="w-6 h-6" />
          </div>
          <div>
            <h1 className="text-2xl font-black tracking-wide text-white">
              ATS <span className="text-acc2">v2</span>
            </h1>
            <p className="text-xs text-muted">Вход в панель управления</p>
          </div>
        </div>

        <form onSubmit={handleSubmit} className="flex flex-col gap-4">
          {needsWorkspace && <div>
            <label className="text-xs font-semibold text-muted uppercase tracking-wider block mb-1.5" htmlFor="workspace-id">ID пространства</label>
            <input id="workspace-id" name="tenant_id" type="text" value={tenantId} onChange={(event) => setTenantId(event.target.value)} className="w-full bg-[#0a1628] border border-line2 rounded-xl px-4 py-2.5 text-sm outline-none focus:border-acc text-white" required />
          </div>}
          <div>
            <label htmlFor="login" className="text-xs font-semibold text-muted uppercase tracking-wider block mb-1.5">
              Логин
            </label>
            <div className="relative">
              <User className="w-4 h-4 text-dim absolute left-3.5 top-3" />
              <input
                id="login"
                name="login"
                type="text"
                value={login}
                onChange={(e) => setLogin(e.target.value)}
                placeholder="admin"
                className="w-full bg-[#0a1628] border border-line2 rounded-xl pl-10 pr-4 py-2.5 text-sm outline-none focus:border-acc focus:ring-2 focus:ring-acc/20 text-white transition-all"
              />
            </div>
          </div>

          <div>
            <label htmlFor="password" className="text-xs font-semibold text-muted uppercase tracking-wider block mb-1.5">
              Пароль
            </label>
            <div className="relative">
              <Lock className="w-4 h-4 text-dim absolute left-3.5 top-3" />
              <input
                id="password"
                name="password"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="••••••••"
                className="w-full bg-[#0a1628] border border-line2 rounded-xl pl-10 pr-4 py-2.5 text-sm outline-none focus:border-acc focus:ring-2 focus:ring-acc/20 text-white transition-all"
              />
            </div>
          </div>

          {error && <div className="text-xs text-bad font-medium mt-1">{error}</div>}

          <button
            type="submit"
            disabled={loading}
            className="w-full mt-2 py-3 rounded-xl bg-gradient-to-r from-[#5b8cff] to-[#3d6bff] hover:brightness-110 font-bold text-sm text-white shadow-lg shadow-acc/30 flex items-center justify-center gap-2 transition-all disabled:opacity-50"
          >
            {loading ? 'Проверка...' : 'Войти в систему'}
            <ArrowRight className="w-4 h-4" />
          </button>
        </form>
      </div>
    </div>
  );
}
