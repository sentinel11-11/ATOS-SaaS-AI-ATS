import React from 'react';
import { afterEach, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import NumberPool from './pages/NumberPool';
import { api } from './api';

vi.mock('./api', () => ({ api: vi.fn() }));
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.resetAllMocks();
});

const number = {
  id: 'multicom-number', number: '9990000101', provider: 'multicom_ami', kind: 'sip',
  active: true, verified: true, enabled_outgoing: true, daily_limit: 30, weight: 1,
  cooldown_seconds: 60, cooling: true, cooldown_remaining_seconds: 5,
};

it.each(['add', 'edit'])('preserves an explicit zero cooldown when saving %s', async (mode) => {
  api.mockImplementation(async (path) => path === '/numbers' ? { numbers: [number] } : { ok: true });
  render(<NumberPool addToast={vi.fn()} />);
  if (mode === 'add') {
    fireEvent.click(screen.getByRole('button', { name: 'Добавить номер в пул' }));
    fireEvent.change(screen.getByPlaceholderText('79260000000'), { target: { value: '+79990000102' } });
  } else {
    fireEvent.click(await screen.findByTitle('Настройки карточки номера'));
  }
  const cooldown = screen.getByLabelText('Пауза между попытками, секунд');
  expect(cooldown.value).toBe('60');
  fireEvent.change(cooldown, { target: { value: '0' } });
  fireEvent.click(screen.getByRole('button', { name: mode === 'add' ? 'Сохранить номер' : 'Сохранить настройки' }));
  await waitFor(() => expect(api).toHaveBeenCalledWith('/numbers/save', {
    body: expect.objectContaining({ cooldown_seconds: 0 }),
  }));
});

it('explains an invalid cooldown before submitting the number', async () => {
  api.mockResolvedValue({ numbers: [number] });
  const addToast = vi.fn();
  render(<NumberPool addToast={addToast} />);
  fireEvent.click(await screen.findByTitle('Настройки карточки номера'));
  fireEvent.change(screen.getByLabelText('Пауза между попытками, секунд'), { target: { value: '-1' } });
  fireEvent.click(screen.getByRole('button', { name: 'Сохранить настройки' }));
  expect(addToast).toHaveBeenCalledWith(expect.stringContaining('от 0 до 86400'), 'warn');
  expect(api.mock.calls.some(([path]) => path === '/numbers/save')).toBe(false);
});

it('refreshes cooldown eligibility without cached reads or replacing an unsaved edit', async () => {
  vi.useFakeTimers();
  let current = number;
  api.mockImplementation(async () => ({ numbers: [current] }));
  await act(async () => { render(<NumberPool addToast={vi.fn()} />); });
  expect(screen.getByText('Остывает · 5 с')).toBeTruthy();
  fireEvent.click(screen.getByTitle('Настройки карточки номера'));
  fireEvent.change(screen.getByLabelText('Пауза между попытками, секунд'), { target: { value: '120' } });
  current = { ...number, cooling: false, cooldown_remaining_seconds: 0 };
  await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
  expect(screen.queryByText(/Остывает/)).toBeNull();
  expect(screen.getByLabelText('Пауза между попытками, секунд').value).toBe('120');
  expect(api).toHaveBeenLastCalledWith('/numbers', { cache: false });
});

it('lets a slow pool read finish before starting another background refresh', async () => {
  vi.useFakeTimers();
  let finishRead;
  api.mockReturnValue(new Promise((resolve) => { finishRead = resolve; }));
  await act(async () => { render(<NumberPool addToast={vi.fn()} />); });
  await act(async () => { await vi.advanceTimersByTimeAsync(10000); });
  expect(api).toHaveBeenCalledTimes(1);
  await act(async () => { finishRead({ numbers: [number] }); });
  expect(screen.getByText(number.number)).toBeTruthy();
});

it.each(['add', 'edit'])('lets an administrator independently configure quota and post-limit cooldown for %s', async (mode) => {
  api.mockImplementation(async (path) => path === '/numbers' ? { numbers: [number] } : { ok: true });
  render(<NumberPool addToast={vi.fn()} />);
  if (mode === 'add') {
    fireEvent.click(screen.getByRole('button', { name: 'Добавить номер в пул' }));
    fireEvent.change(screen.getByPlaceholderText('79260000000'), { target: { value: '+79990000102' } });
  } else {
    fireEvent.click(await screen.findByTitle('Настройки карточки номера'));
  }
  expect(screen.getByLabelText('Охлаждение после лимита, секунд').value).toBe('3600');
  fireEvent.change(screen.getByLabelText('Лимит до охлаждения'), { target: { value: '73' } });
  fireEvent.change(screen.getByLabelText('Охлаждение после лимита, секунд'), { target: { value: '7200' } });
  fireEvent.click(screen.getByRole('button', { name: mode === 'add' ? 'Сохранить номер' : 'Сохранить настройки' }));
  await waitFor(() => expect(api).toHaveBeenCalledWith('/numbers/save', {
    body: expect.objectContaining({ daily_limit: 73, quota_cooldown_seconds: 7200, cooldown_seconds: 60 }),
  }));
});
