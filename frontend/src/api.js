// The original UI contract is retained; authentication belongs to the new core.
// Session credentials stay in HttpOnly cookies, never browser storage or URLs.
let currentUser = null;
const pendingReads = new Map();

export function getToken() {
  return currentUser ? 'cookie-session' : '';
}

export function getUser() {
  return currentUser;
}

export function setSession(authenticated, user) {
  currentUser = authenticated && user ? user : null;
  // Remove credentials left behind by an older frontend on the same origin.
  try {
    localStorage.removeItem('ats_token');
    localStorage.removeItem('ats_user');
  } catch { /* Storage can be disabled; cookie authentication still works. */ }
}

function csrfToken() {
  const cookie = document.cookie.split('; ').find((part) => part.startsWith('ats_csrf='));
  return cookie ? decodeURIComponent(cookie.slice('ats_csrf='.length)) : '';
}

export async function fetchTicket(purpose = 'sse', resource = '') {
  const result = await api('/auth/ticket', { body: { purpose, resource } });
  return result?.ok ? result.ticket : null;
}

export async function waitForJob(result, { signal } = {}) {
  const deadline = Date.now() + 90000;
  while (result?.queued && result.job_id) {
    if (signal?.aborted) return { ok: false, error: 'cancelled' };
    if (Date.now() >= deadline) return { ok: false, error: 'Проверка ещё выполняется. Результат доступен в логах.' };
    await new Promise((resolve) => setTimeout(resolve, 1000));
    result = await api(`/jobs/${encodeURIComponent(result.job_id)}`, { signal });
  }
  return result;
}

export function openWithTicket(path) {
  // Same-origin cookies authorize downloads without exposing credentials in URLs.
  if (!path.startsWith('/api/v2/export/')) return;
  window.open(path, '_blank', 'noopener,noreferrer');
}

export function api(path, options = {}) {
  const method = options.method || (options.body ? 'POST' : 'GET');
  const url = path.startsWith('/api/') ? path : `/api/v2${path.startsWith('/') ? path : '/' + path}`;
  const key = method === 'GET' && !options.signal ? url : null;
  if (key && pendingReads.has(key)) return pendingReads.get(key);

  const request = (async () => {
    const headers = { ...options.headers };
    if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) headers['X-CSRF-Token'] = csrfToken();
    let body = options.body;
    if (body && typeof body === 'object' && !(body instanceof FormData)) {
      headers['Content-Type'] = 'application/json';
      body = JSON.stringify(body);
    }
    try {
      const response = await fetch(url, {
        method, headers, body, signal: options.signal, credentials: 'same-origin',
      });
      const result = (response.headers.get('content-type') || '').includes('application/json')
        ? await response.json()
        : { ok: response.ok, status: response.status };
      if (response.status === 401 && !url.endsWith('/auth/login')) {
        setSession('', null);
        window.dispatchEvent(new CustomEvent('ats_unauthorized'));
      }
      return { ...result, ok: result.ok ?? response.ok };
    } catch (error) {
      return { ok: false, error: error.name === 'AbortError' ? 'cancelled' : 'network_unavailable' };
    }
  })();

  if (key) {
    pendingReads.set(key, request);
    request.finally(() => pendingReads.delete(key));
  }
  return request;
}
