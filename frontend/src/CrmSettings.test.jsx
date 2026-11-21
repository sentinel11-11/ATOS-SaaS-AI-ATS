import React from 'react';
import {afterEach, expect, it, vi} from 'vitest';
import {cleanup, fireEvent, render, screen, waitFor, within} from '@testing-library/react';
import Settings from './pages/Settings';
import {api} from './api';
import {summarizeSaveResults} from './settingsSave';

vi.mock('./api', () => ({api: vi.fn(), waitForJob: vi.fn(async result => result)}));
afterEach(cleanup);
afterEach(() => vi.restoreAllMocks());

function respond(path, options) {
  if (path === '/settings/raw' && !options?.body) return {provider_config: {
    crm: {driver: 'amocrm'}, amocrm: {subdomain: 'omkr', account_id: ''}, multicom: {account_id: ''},
  }};
  if (path === '/settings') return {settings: {provider: 'multicom_ami'}};
  if (path === '/amocrm/check') return {ok: true, connected: true, report: {account_id: 12345678}};
  return {ok: true};
}

it('keeps an unconfigured account ID blank and fills it after a verified CRM check', async () => {
  api.mockImplementation(respond);
  const addToast = vi.fn();
  render(<Settings addToast={addToast} />);
  const account = await screen.findByLabelText('ID аккаунта amoCRM');
  fireEvent.change(account, {target: {value: '123'}});
  fireEvent.change(account, {target: {value: ''}});
  expect(account.value).toBe('');
  const panel = screen.getByText('Интеграция с CRM').closest('.glass-panel');
  fireEvent.click(within(panel).getByRole('button', {name: 'Проверить связь'}));
  await waitFor(() => expect(account.value).toBe('12345678'));
  expect(api).toHaveBeenCalledWith('/settings/raw', {body: {provider_config: expect.objectContaining({
    amocrm: expect.objectContaining({account_id: ''}), multicom: {account_id: ''},
  })}});
  expect(addToast).toHaveBeenCalledWith('Успешно: Проверка связи с amoCRM', 'ok');
});

it('shows the validation detail instead of an ambiguous provider error code', () => {
  const result = summarizeSaveResults({ok: false, error: 'invalid_provider_number', detail: 'Некорректное числовое поле: amocrm.account_id.'}, {ok: true});
  expect(result.failedSections[0].error).toBe('Некорректное числовое поле: amocrm.account_id.');
});
