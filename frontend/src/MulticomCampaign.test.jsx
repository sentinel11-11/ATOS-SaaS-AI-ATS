import React from 'react';
import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import Campaigns from './pages/Campaigns';
import NumberPool from './pages/NumberPool';
import Settings from './pages/Settings';
import TimelineModal from './components/TimelineModal';
import { api } from './api';

vi.mock('./api', () => ({ api: vi.fn(), waitForJob: vi.fn(async (result) => result) }));
afterEach(cleanup);
afterEach(() => vi.restoreAllMocks());

it('creates a Multicom message campaign with one channel and zero retries', async () => {
  api.mockImplementation(async (path) => {
    if (path === '/templates') return { templates: [{ id: 'template-1', name: 'Existing template' }] };
    if (path === '/campaigns/save') return { ok: true };
    return { campaigns: [], contacts: [], databases: [] };
  });
  render(<Campaigns addToast={vi.fn()} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Новая кампания' }));
  expect(screen.getByLabelText('Провайдер телефонии').value).toBe('multicom_ami');
  expect(screen.getByLabelText('Тип сценария').value).toBe('message');
  expect(screen.getByLabelText('Шаблон / Голосовое сообщение').value).toBe('');
  expect(screen.getByLabelText('Повторных попыток').value).toBe('0');
  expect(screen.getByLabelText('Одновременных каналов').value).toBe('1');
  fireEvent.click(screen.getByRole('button', { name: 'Сохранить кампанию' }));
  await waitFor(() => expect(api).toHaveBeenCalledWith('/campaigns/save', {
    body: expect.objectContaining({
      route: 'multicom_ami', flow: 'message', template_id: null, retry_max: 0, max_channels: 1,
    }),
  }));
});

it('requires confirmation before activating an imported Multicom caller number', async () => {
  const number = {
    id: 'multicom-number', number: '9690229926', provider: 'multicom_ami', kind: 'sip',
    active: false, verified: false, enabled_outgoing: true, daily_limit: 30, weight: 1,
  };
  api.mockImplementation(async (path) => {
    if (path === '/numbers') return { numbers: [number] };
    return { ok: true };
  });
  const addToast = vi.fn();
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
  render(<NumberPool addToast={addToast} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Включить' }));
  expect(addToast).toHaveBeenCalledWith(expect.stringContaining('подтвердите'), 'warn');
  expect(api.mock.calls.some(([path]) => path === '/numbers/save')).toBe(false);
  fireEvent.click(screen.getByTitle('Настройки карточки номера'));
  fireEvent.click(screen.getByLabelText('А-номер подтверждён у оператора'));
  expect(confirm).toHaveBeenCalledOnce();
  fireEvent.click(screen.getByLabelText('Активен (участвует в ротации исходящих)'));
  fireEvent.click(screen.getByRole('button', { name: 'Сохранить настройки' }));
  await waitFor(() => expect(api).toHaveBeenCalledWith('/numbers/save', {
    body: expect.objectContaining({ id: number.id, provider: 'multicom_ami', active: true, verified: true }),
  }));
});

it('shows the reason ATOS blocks campaign start', async () => {
  const detail = 'Подтвердите активный А-номер в его карточке в пуле после проверки у Мультикома.';
  api.mockImplementation(async (path) => {
    if (path === '/campaigns') return { campaigns: [{ id: 'campaign-1', name: 'Test', status: 'paused', flow: 'message' }] };
    if (path === '/campaigns/campaign-1/start') return { ok: false, error: 'caller_number_not_verified', detail };
    return { templates: [], contacts: [], databases: [] };
  });
  const addToast = vi.fn();
  render(<Campaigns addToast={addToast} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Запустить' }));
  await waitFor(() => expect(addToast).toHaveBeenCalledWith(detail, 'err'));
});

it('reports successful AMI connectivity with a blocked ATOS route as a warning', async () => {
  const detail = 'Подтвердите активный А-номер в его карточке в пуле после проверки у Мультикома.';
  api.mockImplementation(async (path, options) => {
    if (path === '/settings/raw' && !options?.body) return { provider_config: { ami: {} } };
    if (path === '/settings') return { settings: { provider: 'multicom_ami' } };
    if (path === '/ami/check') return {
      ok: true, connected: true,
      report: {
        route_ready: false, blocked_reason: 'caller_number_not_verified',
        checks: [{ name: 'Допуск маршрута', ok: false, critical: true, detail }],
      },
    };
    return { ok: true, users: [], pipelines: [] };
  });
  const addToast = vi.fn();
  render(<Settings addToast={addToast} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Проверить AMI и допуск маршрута' }));
  await waitFor(() => expect(addToast).toHaveBeenCalledWith('AMI подключён. ' + detail, 'warn'));
  expect(await screen.findByText(detail)).toBeTruthy();
});

it('shows a stored failure reason for an existing call in the timeline', async () => {
  api.mockResolvedValue({
    call: { id: 'failed-call', status: 'failed', caller_id: '9690229926', detail: 'ami_endpoint_unavailable' },
    events: [{ event_type: 'failed', fingerprint: 'event-1', received_at: '2026-11-21T12:17:02', hangup_cause: 21 }],
  });
  render(<TimelineModal callId="failed-call" onClose={vi.fn()} />);
  expect(await screen.findByText('ami_endpoint_unavailable')).toBeTruthy();
  expect(screen.getByRole('alert').textContent).toContain('PJSIP endpoint');
  expect(screen.getByText('Код завершения Asterisk 21')).toBeTruthy();
});

it('saves the Multicom endpoint prefix through the settings form', async () => {
  api.mockImplementation(async (path, options) => {
    if (path === '/settings/raw' && !options?.body) return {
      provider_config: { ami: { trunk: 'mcm', cli_endpoint_prefix: '' } },
    };
    if (path === '/settings') return { settings: { provider: 'multicom_ami' } };
    return { ok: true, users: [], pipelines: [] };
  });
  render(<Settings addToast={vi.fn()} />);
  const prefix = await screen.findByLabelText('Префикс endpoint А-номера (ротация)');
  expect(screen.getByText(/Пустой префикс использует фиксированный endpoint mcm/)).toBeTruthy();
  fireEvent.change(prefix, { target: { value: 'mcm-cli-' } });
  fireEvent.click(screen.getByRole('button', { name: 'Сохранить все настройки' }));
  await waitFor(() => expect(api).toHaveBeenCalledWith('/settings/raw', {
    body: { provider_config: { ami: { trunk: 'mcm', cli_endpoint_prefix: 'mcm-cli-' } } },
  }));
});

it('saves the bounded Echo answer context through the settings form', async () => {
  api.mockImplementation(async (path, options) => {
    if (path === '/settings/raw' && !options?.body) return {
      provider_config: { ami: { answer_context: 'ats-beta-answer' } },
    };
    if (path === '/settings') return { settings: { provider: 'multicom_ami' } };
    return { ok: true, users: [], pipelines: [] };
  });
  render(<Settings addToast={vi.fn()} />);
  const answerContext = await screen.findByLabelText('Обработка после ответа');
  fireEvent.change(answerContext, { target: { value: 'ats-beta-echo' } });
  expect(screen.getByText(/Echo вернёт ваш голос/)).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: 'Сохранить все настройки' }));
  await waitFor(() => expect(api).toHaveBeenCalledWith('/settings/raw', {
    body: { provider_config: { ami: { answer_context: 'ats-beta-echo' } } },
  }));
});
