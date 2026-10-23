import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { api, getToken, getUser, openWithTicket, setSession, waitForJob } from './api';

const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

beforeEach(() => {
  setSession('', null);
  document.cookie = 'ats_csrf=test-csrf; path=/';
});
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); vi.useRealTimers(); });

describe('legacy API contract with core sessions', () => {
  it('sends cookie credentials and CSRF without bearer tokens or browser persistence', async () => {
    localStorage.setItem('ats_token', 'obsolete-secret');
    setSession('cookie', { id: 'user-id', login: 'admin' });
    expect(getToken()).toBe('cookie-session');
    expect(getUser().login).toBe('admin');
    expect(localStorage.getItem('ats_token')).toBeNull();
    const fetcher = vi.fn().mockResolvedValue(json({ ok: true }));
    vi.stubGlobal('fetch', fetcher);
    await api('/contacts/save', { body: { name: 'Тест', phone: '+79991234567' } });
    const [url, options] = fetcher.mock.calls[0];
    expect(url).toBe('/api/v2/contacts/save');
    expect(options.credentials).toBe('same-origin');
    expect(options.headers['X-CSRF-Token']).toBe('test-csrf');
    expect(options.headers['X-Ats-Token']).toBeUndefined();
    expect(options.body).toContain('Тест');
  });

  it('coalesces concurrent reads, then refetches after completion', async () => {
    let resolve;
    const fetcher = vi.fn().mockImplementationOnce(() => new Promise((done) => { resolve = done; }))
      .mockResolvedValue(json({ ok: true, contacts: [] }));
    vi.stubGlobal('fetch', fetcher);
    const first = api('/contacts');
    const second = api('/contacts');
    expect(fetcher).toHaveBeenCalledTimes(1);
    resolve(json({ ok: true, contacts: [{ id: 'opaque-uuid' }] }));
    expect(await first).toEqual(await second);
    await api('/contacts');
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it('revokes local identity on expired sessions but keeps login failures local', async () => {
    const listener = vi.fn();
    window.addEventListener('ats_unauthorized', listener);
    vi.stubGlobal('fetch', vi.fn().mockImplementation(() => Promise.resolve(json({ error: 'invalid_credentials' }, 401))));
    setSession(true, { id: 'one' });
    await api('/auth/login', { body: { login: 'admin', password: 'wrong' } });
    expect(listener).not.toHaveBeenCalled();
    await api('/contacts');
    expect(listener).toHaveBeenCalledTimes(1);
    expect(getUser()).toBeNull();
    window.removeEventListener('ats_unauthorized', listener);
  });

  it('waits for queued provider checks and propagates their failure', async () => {
    vi.useFakeTimers();
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json({ ok: false, queued: false, status: 'dead', error: 'connection_failed' })));
    const result = waitForJob({ ok: true, queued: true, job_id: 'check-id' });
    await vi.advanceTimersByTimeAsync(1000);
    expect(await result).toMatchObject({ ok: false, error: 'connection_failed' });
  });

  it('opens only local exports without session credentials in the URL', () => {
    const open = vi.spyOn(window, 'open').mockImplementation(() => null);
    openWithTicket('/api/v2/export/contacts.csv');
    openWithTicket('https://example.com/export');
    expect(open).toHaveBeenCalledTimes(1);
    expect(open.mock.calls[0][0]).toBe('/api/v2/export/contacts.csv');
  });
});
