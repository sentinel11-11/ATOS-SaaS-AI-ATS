import React from 'react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import AuthGate from './pages/AuthGate';
import { api, setSession } from './api';
vi.mock('./api', () => ({ api: vi.fn(), setSession: vi.fn() }));
afterEach(cleanup);
beforeEach(() => vi.resetAllMocks());

it('retains the original login form and only requests workspace identity when required', async () => {
  api.mockResolvedValueOnce({ ok: false, error: 'tenant_required' })
    .mockResolvedValueOnce({ ok: true, user_id: 'user-uuid', tenant_id: 'tenant-uuid', login: 'admin', role: 'admin' });
  const login = vi.fn();
  render(<AuthGate onLoginSuccess={login} />);
  expect(screen.queryByLabelText('ID пространства')).toBeNull();
  fireEvent.change(screen.getByLabelText('Пароль'), { target: { value: 'test-password-12' } });
  fireEvent.click(screen.getByRole('button', { name: 'Войти в систему' }));
  await screen.findByLabelText('ID пространства');
  fireEvent.change(screen.getByLabelText('ID пространства'), { target: { value: 'tenant-uuid' } });
  fireEvent.click(screen.getByRole('button', { name: 'Войти в систему' }));
  await waitFor(() => expect(login).toHaveBeenCalledWith(expect.objectContaining({ id: 'user-uuid', tenant_id: 'tenant-uuid' })));
  expect(api.mock.calls[1][1].body.tenant_id).toBe('tenant-uuid');
  expect(setSession).toHaveBeenCalledWith(true, expect.objectContaining({ login: 'admin' }));
});
