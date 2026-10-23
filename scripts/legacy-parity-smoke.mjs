/** Actual browser regression against the original ZIP UI and the new PostgreSQL core.
 * Run with API + four simulator workers on localhost:8017 and ATS_DATABASE_URL=ats_smoke*.
 * Optionally set ATS_LEGACY_REFERENCE_DIST to a build of the unchanged original frontend.
 * Synthetic accounts only. No SIP/AMI/CRM requests are made by this script.
 */
import { spawn, spawnSync } from 'node:child_process';
import { createServer } from 'node:http';
import { mkdtemp, readFile, writeFile, mkdir, rm } from 'node:fs/promises';
import { createHash, randomUUID, randomBytes } from 'node:crypto';
import { tmpdir } from 'node:os';
import { join, resolve, extname, sep } from 'node:path';

const origin = process.env.ATS_BROWSER_ORIGIN || 'http://127.0.0.1:8017';
if (!['127.0.0.1', 'localhost'].includes(new URL(origin).hostname)) throw Error('Loopback smoke only');
const contract = JSON.parse(await readFile('tests/fixtures/legacy-ui-contract.json', 'utf8'));
const ready = await (await fetch(origin + '/health/ready')).json();
if (ready.live_calls !== false || ready.status !== 'ready') throw Error('Simulator must be ready, live calls disabled');
const credentials = { tenant_id: randomUUID(), login: 'parity-' + randomBytes(4).toString('hex'), password: randomBytes(24).toString('base64url') };
const created = spawnSync('.venv/bin/python', ['-c', `import json,sys,os; from uuid import UUID; from sqlalchemy.engine import make_url; from ats.cli import bootstrap; from ats.db import session_factory; assert (make_url(os.environ['ATS_DATABASE_URL']).database or '').startswith('ats_smoke'); d=json.load(sys.stdin); bootstrap(session_factory(),UUID(d['tenant_id']),'Legacy UI regression',d['login'],d['password'])`], { input: JSON.stringify(credentials), encoding: 'utf8' });
if (created.status !== 0) throw Error('Disposable ats_smoke account could not be created');
const artifacts = 'artifacts/legacy-parity';
await mkdir(artifacts, { recursive: true });
const profile = await mkdtemp(join(tmpdir(), 'ats-parity-browser-'));
const chrome = spawn(process.env.CHROMIUM || '/usr/bin/chromium', ['--headless', '--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage', '--remote-debugging-port=0', '--user-data-dir=' + profile, 'about:blank'], { stdio: 'ignore' });
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const report = { version: ready.version, reference: contract.reference, browser: 'Chromium', live_telephony_tested: false, screens: [], actions: [], visual_comparison: [], source_preservation: [], javascript_errors: [], api_failures: [] };
let ws; let referenceServer;
try {
  let port;
  for (let i = 0; i < 100; i++) { try { port = Number((await readFile(join(profile, 'DevToolsActivePort'), 'utf8')).split('\n')[0]); break; } catch { await delay(100); } }
  if (!port) throw Error('Chromium startup timeout');
  const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
  ws = new WebSocket(targets.find(p => p.type === 'page').webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { ws.addEventListener('open', resolve, { once: true }); ws.addEventListener('error', reject, { once: true }); });
  let sequence = 0;
  const waiting = new Map();
  const network = new Map();
  ws.addEventListener('message', event => {
    const message = JSON.parse(event.data);
    if (message.id) { const callback = waiting.get(message.id); waiting.delete(message.id); callback?.(message); }
    else if (message.method === 'Runtime.exceptionThrown') report.javascript_errors.push(message.params.exceptionDetails.text);
    else if (message.method === 'Network.requestWillBeSent') network.set(message.params.requestId, { method: message.params.request.method, path: new URL(message.params.request.url).pathname, started: message.params.timestamp });
    else if (message.method === 'Network.responseReceived') {
      const req = network.get(message.params.requestId);
      if (req && req.path.startsWith('/api/')) {
        req.status = message.params.response.status;
        req.ms = Math.round((message.params.timestamp - req.started) * 1000);
        if (req.status >= 400 && !req.path.endsWith('/auth/me')) report.api_failures.push({ path: req.path, status: req.status });
      }
    }
  });
  const cdp = (method, params = {}) => new Promise((resolve, reject) => { const id = ++sequence; const timer = setTimeout(() => { waiting.delete(id); reject(Error('CDP timeout: ' + method)); }, 15000); waiting.set(id, result => { clearTimeout(timer); result.error ? reject(Error(result.error.message)) : resolve(result.result); }); ws.send(JSON.stringify({ id, method, params })); });
  const evaluate = async expression => { const result = await cdp('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true }); if (result.exceptionDetails) throw Error('Browser expression failed: ' + (result.exceptionDetails.exception?.description || result.exceptionDetails.text)); return result.result.value; };
  const wait = async (expression, label) => { for (let i = 0; i < 150; i++) { if (await evaluate(expression)) return; await delay(100); } throw Error('Browser timeout: ' + label); };
  const visible = `e=>!!(e.offsetWidth||e.offsetHeight||e.getClientRects().length)`;
  const click = (text, scope = 'document') => evaluate(`(()=>{const b=Array.from(${scope}.querySelectorAll('button')).filter(${visible}).find(e=>e.textContent.trim()===${JSON.stringify(text)});if(!b)throw Error('Missing button: '+${JSON.stringify(text)});b.click();})()`);
  const fillLabel = (text, value) => evaluate(`(()=>{const l=Array.from(document.querySelectorAll('label')).filter(${visible}).find(e=>e.textContent.trim().startsWith(${JSON.stringify(text)}));if(!l)throw Error('Missing label: '+${JSON.stringify(text)});const e=(l.htmlFor&&document.getElementById(l.htmlFor))||l.parentElement.querySelector('input,textarea,select');if(!e)throw Error('Missing labelled control');const p=e.tagName==='SELECT'?HTMLSelectElement.prototype:e.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;Object.getOwnPropertyDescriptor(p,'value').set.call(e,${JSON.stringify(value)});e.dispatchEvent(new Event(e.tagName==='SELECT'?'change':'input',{bubbles:true}));})()`);
  const closeModal = () => evaluate(`(()=>{const h=Array.from(document.querySelectorAll('.fixed h2')).filter(${visible}).at(-1);if(!h)throw Error('No modal to close');h.parentElement.querySelector('button').click();})()`);
  const read = path => evaluate(`fetch(${JSON.stringify('/api/v2' + path)}).then(r=>r.json())`);
  const screenshot = async (name) => { await evaluate('window.scrollTo(0,0)'); await delay(240); const result = await cdp('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false }); await writeFile(`${artifacts}/${name}.png`, Buffer.from(result.data, 'base64')); };
  const action = async (name, run) => { const start = performance.now(); await run(); report.actions.push({ name, status: 'passed', duration_ms: Math.round(performance.now() - start) }); };
  const navigate = async (page) => { await click(page.navigation, "document.querySelector('aside nav')"); await wait(`!!document.querySelector('main') && !document.querySelector('main').innerText.includes('Загрузка')`, page.navigation); await delay(150); };
  await cdp('Runtime.enable'); await cdp('Page.enable'); await cdp('Network.enable');
  await cdp('Emulation.setDeviceMetricsOverride', { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false });
  await cdp('Emulation.setTimezoneOverride', { timezoneId: 'Europe/Moscow' });
  await cdp('Page.addScriptToEvaluateOnNewDocument', { source: `window.confirm=()=>true;window.__downloads=[];window.open=(url)=>{fetch(url).then(async r=>window.__downloads.push({status:r.status,type:r.headers.get('content-type'),body:await r.text()}));return null;};` });
  await cdp('Page.navigate', { url: `${origin}/?workspace=${credentials.tenant_id}` });
  await wait(`!!document.querySelector('input[type="password"]')`, 'original login form');
  await action('authenticate_cookie_session', async () => {
    await fillLabel('Логин', credentials.login); await fillLabel('Пароль', credentials.password); await click('Войти в систему');
    await wait(`!!document.querySelector('aside nav')`, 'authenticated original workspace');
    const cookies = await cdp('Network.getCookies', { urls: [origin] });
    if (!cookies.cookies.some(c => c.name === 'ats_session' && c.httpOnly)) throw Error('Missing HttpOnly session');
    if (await evaluate(`!!localStorage.getItem('ats_token')`)) throw Error('Session token leaked to browser storage');
  });
  await action('save_workspace_settings', async () => {
    await navigate(contract.pages.find(p => p.id === 'settings')); await fillLabel('Провайдер телефонии', 'sim'); await click('Сохранить все настройки');
    await wait(`fetch('/api/v2/settings').then(r=>r.json()).then(d=>(d.settings||d).provider==='sim')`, 'saved simulator provider');
  });
  await action('create_contact', async () => {
    await navigate(contract.pages.find(p => p.id === 'contacts')); await click('Добавить контакт'); await fillLabel('Телефон', '+79990000661'); await fillLabel('Имя клиента', 'Parity contact'); await fillLabel('Группа', 'Browser fixture'); await click('Сохранить');
    await wait(`Array.from(document.querySelectorAll('tbody tr')).some(r=>r.innerText.includes('Parity contact'))`, 'saved contact row');
  });
  await action('open_contact_history', async () => { await click('Карточка'); await wait(`document.querySelector('.fixed h2')?.innerText.includes('Карточка')`, 'contact card'); await closeModal(); });
  await action('toggle_contact_consent', async () => {
    await click('Есть (Вкл)'); await wait(`Array.from(document.querySelectorAll('button')).some(b=>b.innerText.trim()==='Нет (Выкл)')`, 'consent withdrawn'); await click('Нет (Выкл)'); await wait(`Array.from(document.querySelectorAll('button')).some(b=>b.innerText.trim()==='Есть (Вкл)')`, 'consent restored');
  });
  await action('import_csv_file', async () => {
    await click('Импорт из CSV / Excel'); await evaluate(`(()=>{const e=document.querySelector('input[type="file"]');const d=new DataTransfer();d.items.add(new File(['phone,name\\n+79990000662,CSV parity contact\\n'],'browser-fixture.csv',{type:'text/csv'}));e.files=d.files;e.dispatchEvent(new Event('change',{bubbles:true}));})()`);
    await click('Загрузить базу'); await wait(`document.body.innerText.includes('Результаты импорта') || document.body.innerText.includes('Отчёт об импорте') || document.body.innerText.includes('CSV parity contact')`, 'CSV result');
    if (await evaluate(`!!document.querySelector('.fixed h2')`)) await closeModal();
    await wait(`Array.from(document.querySelectorAll('tbody tr')).some(r=>r.innerText.includes('CSV parity contact'))`, 'imported contact row');
  });
  await action('export_contacts_csv', async () => { await click('Экспорт контактов в CSV'); await wait(`window.__downloads.some(d=>d.status===200&&d.body.includes('Parity contact'))`, 'CSV contacts download'); });
  await action('create_template', async () => { await navigate(contract.pages.find(p => p.id === 'templates')); await click('Новый шаблон'); await fillLabel('Название шаблона', 'Parity message'); await fillLabel('Основной текст', 'Проверка интерфейса на симуляторе.'); await click('Сохранить шаблон'); await wait(`Array.from(document.querySelectorAll('tbody tr')).some(r=>r.innerText.includes('Parity message'))`, 'saved template'); });
  await action('edit_template', async () => { await click('Редактировать'); await fillLabel('Название шаблона', 'Parity message edited'); await click('Сохранить шаблон'); await wait(`Array.from(document.querySelectorAll('tbody tr')).some(r=>r.innerText.includes('Parity message edited'))`, 'edited template'); });
  await action('create_campaign', async () => { await navigate(contract.pages.find(p => p.id === 'campaigns')); await click('Новая кампания'); await fillLabel('Название кампании', 'Parity simulator campaign'); await fillLabel('Тип сценария', 'message'); await fillLabel('Рабочее время', '00:00-24:00'); await click('Сохранить кампанию'); await wait(`document.querySelector('main').innerText.includes('Parity simulator campaign') && !document.querySelector('.fixed h2')`, 'saved campaign'); });
  await action('add_selected_contacts_to_campaign', async () => { await click('Карточка кампании'); await click('Добавить контакты'); await click('Добавить ВСЕ с согласием (152-ФЗ)'); await wait(`!document.querySelector('.fixed h2') && document.querySelector('main').innerText.includes('+79990000661')`, 'campaign queue member'); });
  await action('start_simulator_campaign', async () => { await click('Запустить'); await wait(`fetch('/api/v2/calls',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':decodeURIComponent(document.cookie.split('; ').find(s=>s.startsWith('ats_csrf=')).split('=').slice(1).join('='))},body:JSON.stringify({only_campaigns:true})}).then(r=>r.json()).then(d=>d.calls?.some(c=>['done','completed'].includes(c.status)))`, 'separate workers completed simulator call'); });
  await action('view_call_timeline', async () => { await navigate(contract.pages.find(p => p.id === 'journal')); await click('ВАТС Логи'); await wait(`document.querySelector('.fixed h2')?.innerText.includes('Хронология') && document.body.innerText.includes('События ВАТС')`, 'journal timeline'); await screenshot('timeline'); await closeModal(); });
  await action('export_calls_csv', async () => { await click('Экспорт в CSV'); await wait(`window.__downloads.some(d=>d.status===200&&d.body.includes('Parity contact')&&d.body.includes('campaign'))`, 'CSV calls download'); });
  await action('create_caller_number', async () => { await navigate(contract.pages.find(p => p.id === 'numbers')); await click('Добавить номер в пул'); await fillLabel('Номер телефона', '9990000663'); await fillLabel('Метка', 'Parity CLI'); await click('Сохранить номер'); await wait(`!document.querySelector('.fixed h2') && document.querySelector('main').innerText.includes('Parity CLI')`, 'caller pool row'); });
  await action('quarantine_caller_number', async () => { await click('В Карантин'); await wait(`document.querySelector('main').innerText.includes('Карантин (ЧС)')`, 'quarantined caller'); });
  await action('reset_caller_number_counters', async () => { await click('Сбросить счётчики'); await wait(`document.body.innerText.includes('сброшены')`, 'caller number reset response'); });
  await action('create_blacklist_entry', async () => { await navigate(contract.pages.find(p => p.id === 'blacklist')); await click('Добавить в ЧС'); await fillLabel('Телефон', '+79990000664'); await fillLabel('Причина', 'Synthetic exclusion'); await click('Заблокировать'); await wait(`Array.from(document.querySelectorAll('tbody tr')).some(r=>r.innerText.includes('Synthetic exclusion'))`, 'blacklist row'); });
  await action('delete_blacklist_entry', async () => { await evaluate(`document.querySelector('button[title="Удалить из ЧС"]').click()`); await wait(`!Array.from(document.querySelectorAll('tbody tr')).some(r=>r.innerText.includes('Synthetic exclusion'))`, 'removed blacklist row'); });
  await action('create_operator_user', async () => { await navigate(contract.pages.find(p => p.id === 'settings')); await click('Новый пользователь / оператор'); await fillLabel('Логин (для входа)', 'parity-operator'); await fillLabel('Имя оператора', 'Parity operator'); await fillLabel('Внутренний номер', '171'); await fillLabel('Пароль', randomBytes(24).toString('base64url')); await click('Сохранить пользователя'); await wait(`Array.from(document.querySelectorAll('tbody tr')).some(r=>r.innerText.includes('@parity-operator'))`, 'saved operator user'); });
  await action('filter_audit_logs', async () => { await navigate(contract.pages.find(p => p.id === 'admin-logs')); await click('Применить'); await wait(`!document.querySelector('main').innerText.includes('Загрузка') && !!document.querySelector('tbody tr')`, 'filtered audit log rows'); });
  for (const page of contract.pages) { await navigate(page); await screenshot('restored-' + page.id); report.screens.push({ page: page.id, heading: await evaluate(`document.querySelector('main h1')?.textContent.trim() || document.querySelector('main')?.innerText.split('\\n')[0] || ''`) }); }
  for (const spec of [contract.theme, contract.styles, ...contract.pages]) {
    const path = spec.file || spec.source;
    const bytes = await readFile('frontend/' + path);
    report.source_preservation.push({ file: path, unchanged: createHash('sha256').update(bytes).digest('hex') === spec.sha256 });
  }
  // Source-level layout styles are an explicit gate; source changes to add pagination
  // or adapt authentication are listed, while all original page names are exercised.
  if (!report.source_preservation.find(p => p.file === contract.theme.file)?.unchanged || !report.source_preservation.find(p => p.file === contract.styles.file)?.unchanged) throw Error('Original visual theme changed');
  const referenceDist = process.env.ATS_LEGACY_REFERENCE_DIST;
  if (referenceDist) {
    const cookieRows = (await cdp('Network.getCookies', { urls: [origin] })).cookies;
    const cookie = cookieRows.map(c => c.name + '=' + c.value).join('; ');
    const csrf = cookieRows.find(c => c.name === 'ats_csrf')?.value || '';
    const identity = await read('/auth/me');
    const root = resolve(referenceDist);
    referenceServer = createServer(async (req, res) => {
      try {
        const url = new URL(req.url, 'http://127.0.0.1');
        if (url.pathname === '/api/v2/events') { res.writeHead(200, { 'Content-Type': 'text/event-stream' }); res.write(': reference fixture\n\n'); req.on('close', () => res.end()); return; }
        if (url.pathname === '/api/v2/auth/ticket') { res.writeHead(200, { 'Content-Type': 'application/json' }); res.end(JSON.stringify({ ok: true, ticket: 'reference-only-fixture' })); return; }
        if (url.pathname.startsWith('/api/')) {
          if (req.method !== 'GET' && !(req.method === 'POST' && url.pathname === '/api/v2/calls')) { res.writeHead(405); res.end(); return; }
          const chunks = []; for await (const chunk of req) chunks.push(chunk);
          const data = Buffer.concat(chunks);
          const response = await fetch(origin + req.url, { method: req.method, headers: { Cookie: cookie, Origin: origin, 'Content-Type': 'application/json', 'X-CSRF-Token': csrf }, ...(data.length ? { body: data } : {}) });
          res.writeHead(response.status, { 'Content-Type': response.headers.get('content-type') || 'application/json' }); res.end(Buffer.from(await response.arrayBuffer())); return;
        }
        const file = resolve(root, '.' + (url.pathname === '/' ? '/index.html' : decodeURIComponent(url.pathname)));
        if (!file.startsWith(root + sep)) { res.writeHead(403); res.end(); return; }
        const mime = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml' };
        res.writeHead(200, { 'Content-Type': mime[extname(file)] || 'application/octet-stream' }); res.end(await readFile(file));
      } catch { res.writeHead(500); res.end('Reference fixture error'); }
    });
    await new Promise(resolve => referenceServer.listen(0, '127.0.0.1', resolve));
    const referenceOrigin = 'http://127.0.0.1:' + referenceServer.address().port;
    await cdp('Page.addScriptToEvaluateOnNewDocument', { source: `if(location.origin===${JSON.stringify(referenceOrigin)}){localStorage.setItem('ats_token','reference-only-fixture');localStorage.setItem('ats_user',${JSON.stringify(JSON.stringify({ id: identity.user_id, login: identity.login, role: identity.role }))});}` });
    await cdp('Page.navigate', { url: referenceOrigin }); await wait(`!!document.querySelector('aside nav')`, 'original ZIP reference');
    for (const page of contract.pages) {
      await navigate(page); await screenshot('original-' + page.id);
      const result = spawnSync('compare', ['-metric', 'RMSE', `${artifacts}/original-${page.id}.png`, `${artifacts}/restored-${page.id}.png`, `${artifacts}/difference-${page.id}.png`], { encoding: 'utf8' });
      report.visual_comparison.push({ page: page.id, metric: 'RMSE', value: result.stderr.trim(), original: `original-${page.id}.png`, restored: `restored-${page.id}.png` });
    }
    await cdp('Page.navigate', { url: origin }); await wait(`!!document.querySelector('aside nav')`, 'restored cookie session after reload');
  }
  await action('logout_revokes_session', async () => { await evaluate(`document.querySelector('button[title="Выйти из системы"]').click()`); await wait(`!!document.querySelector('input[type="password"]')`, 'logout login screen'); if ((await evaluate(`fetch('/api/v2/auth/me?probe='+Date.now(),{cache:'no-store'}).then(r=>r.ok)`))) throw Error('Session survived logout'); });
  if (report.javascript_errors.length) throw Error('JavaScript runtime errors');
  if (report.api_failures.length) throw Error('API errors: ' + JSON.stringify(report.api_failures));
  report.api_requests = [...network.values()].filter(n => n.path.startsWith('/api/') && n.status);
  const times = report.api_requests.map(r => r.ms).sort((a, b) => a - b);
  report.api_latency = { count: times.length, median_ms: times[Math.floor(times.length / 2)], p95_ms: times[Math.floor(times.length * 0.95)] };
  report.status = 'passed';
  process.stdout.write(JSON.stringify({ status: report.status, screens: report.screens.length, actions: report.actions.length, api_latency: report.api_latency, live_telephony_tested: false }) + '\n');
} catch (error) {
  report.status = 'failed'; report.failure = error.message;
  throw error;
} finally {
  await writeFile(`${artifacts}/report.json`, JSON.stringify(report, null, 2) + '\n');
  ws?.close(); chrome.kill('SIGTERM'); referenceServer?.closeAllConnections(); referenceServer?.close();
  if (chrome.exitCode === null) await new Promise(resolve => chrome.once('exit', resolve));
  try { await rm(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 }); } catch { /* Chromium can release profile files a moment later. */ }
}
