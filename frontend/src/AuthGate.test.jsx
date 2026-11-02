import React from 'react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import AuthGate from './pages/AuthGate';
import { api, setSession } from './api';
vi.mock('./api', () => ({ api: vi.fn(), setSession: vi.fn() }));
afterEach(cleanup);
beforeEach(() => vi.resetAllMocks());

it('submits workspace, login, and password together', async () => {
  api.mockResolvedValueOnce({ ok: true, user_id: 'user-uuid', tenant_id: 'tenant-uuid', login: 'admin', role: 'admin' });
  const login = vi.fn();
  render(<AuthGate onLoginSuccess={login} />);
  fireEvent.change(screen.getByLabelText('ID пространства'), { target: { value: 'tenant-uuid' } });
  fireEvent.change(screen.getByLabelText('Логин'), { target: { value: 'admin' } });
  fireEvent.change(screen.getByLabelText('Пароль'), { target: { value: 'test-password-12' } });
  fireEvent.click(screen.getByRole('button', { name: 'Войти в систему' }));
  await waitFor(() => expect(login).toHaveBeenCalledWith(expect.objectContaining({ id: 'user-uuid', tenant_id: 'tenant-uuid' })));
  expect(api).toHaveBeenCalledTimes(1);
  expect(api.mock.calls[0][1].body).toEqual({ login: 'admin', password: 'test-password-12', tenant_id: 'tenant-uuid' });
  expect(setSession).toHaveBeenCalledWith(true, expect.objectContaining({ login: 'admin' }));
});

it('keeps credentials filled when the workspace ID is rejected as required', async () => {
  api.mockResolvedValueOnce({ ok: false, error: 'tenant_required' });
  render(<AuthGate />);
  fireEvent.change(screen.getByLabelText('Логин'), { target: { value: 'admin' } });
  fireEvent.change(screen.getByLabelText('Пароль'), { target: { value: 'test-password-12' } });
  fireEvent.click(screen.getByRole('button', { name: 'Войти в систему' }));
  const workspace = await screen.findByLabelText('ID пространства');
  expect(workspace.value).toBe('');
  expect(screen.getByLabelText('Логин').value).toBe('admin');
  expect(screen.getByLabelText('Пароль').value).toBe('test-password-12');
});
