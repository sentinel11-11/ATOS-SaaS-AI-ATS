import React from 'react';
import { afterEach, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import Contacts from './pages/Contacts';
import { api } from './api';

vi.mock('./api', () => ({ api: vi.fn(), openWithTicket: vi.fn() }));
afterEach(cleanup);

const contact = { id: 'contact-1', name: 'Client fixture', phone: '+79855772554', consent: 1 };
const searchPlaceholder = 'Поиск по имени, номеру телефона или группе...';

it('resets pagination and displays server matches for a formatted phone', async () => {
  api.mockImplementation((path) => {
    if (path.startsWith('/contacts?')) {
      const params = new URLSearchParams(path.split('?')[1]);
      return { contacts: [contact], total: params.has('q') ? 1 : 65 };
    }
    return { campaigns: [] };
  });
  render(<Contacts addToast={vi.fn()} />);
  await screen.findByText(contact.name);
  fireEvent.click(screen.getByRole('button', { name: 'Вперёд' }));
  await waitFor(() => expect(api).toHaveBeenCalledWith('/contacts?offset=30&limit=30'));
  fireEvent.change(screen.getByPlaceholderText(searchPlaceholder), { target: { value: '8 985 577 25 54' } });
  await waitFor(() => expect(api).toHaveBeenCalledWith('/contacts?offset=0&limit=30&q=8+985+577+25+54'));
  await waitFor(() => expect(screen.getByText('Страница 1 из 1')).toBeTruthy());
  expect(screen.getByText(contact.name)).toBeTruthy();
  expect(screen.getByText('Всего в базе: 1 контактов')).toBeTruthy();
});

it('keeps newer search results when an older request completes later', async () => {
  let resolveOld;
  api.mockImplementation((path) => {
    if (path.includes('q=old')) return new Promise((resolve) => { resolveOld = resolve; });
    if (path.startsWith('/contacts?')) return { contacts: [contact], total: 1 };
    return { campaigns: [] };
  });
  render(<Contacts addToast={vi.fn()} />);
  await screen.findByText(contact.name);
  fireEvent.change(screen.getByPlaceholderText(searchPlaceholder), { target: { value: 'old' } });
  await waitFor(() => expect(resolveOld).toBeTypeOf('function'));
  fireEvent.change(screen.getByPlaceholderText(searchPlaceholder), { target: { value: 'new' } });
  await waitFor(() => expect(api).toHaveBeenCalledWith('/contacts?offset=0&limit=30&q=new'));
  await act(async () => { resolveOld({ contacts: [], total: 0 }); });
  expect(screen.getByText(contact.name)).toBeTruthy();
});

it('shows a restored contact immediately after saving from a later page', async () => {
  const addToast = vi.fn();
  api.mockImplementation((path) => {
    if (path === '/contacts/save') return { ok: true, id: contact.id, updated_existing: contact.id, result: 'restored' };
    if (path.startsWith('/contacts?')) return { contacts: [contact], total: path.includes('q=') ? 1 : 65 };
    return { campaigns: [] };
  });
  render(<Contacts addToast={addToast} />);
  await screen.findByText(contact.name);
  fireEvent.click(screen.getByRole('button', { name: 'Вперёд' }));
  await waitFor(() => expect(api).toHaveBeenCalledWith('/contacts?offset=30&limit=30'));
  fireEvent.click(screen.getByRole('button', { name: 'Добавить контакт' }));
  fireEvent.change(screen.getByPlaceholderText('79260000000'), { target: { value: contact.phone } });
  fireEvent.click(screen.getByRole('button', { name: 'Сохранить' }));
  await waitFor(() => expect(addToast).toHaveBeenCalledWith('Контакт восстановлен. Настройки согласия и блокировки сохранены', 'ok'));
  await waitFor(() => expect(screen.getByText('Страница 1 из 1')).toBeTruthy());
  expect(screen.getByPlaceholderText(searchPlaceholder).value).toBe(contact.phone);
  expect(screen.getByText(contact.name)).toBeTruthy();
});
