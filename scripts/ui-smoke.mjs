/** Browser smoke using Chromium CDP and Node 24 built-ins; synthetic simulator data only. */
import { spawn, spawnSync } from 'node:child_process';
import { mkdtemp, readFile, writeFile, mkdir, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { randomUUID, randomBytes } from 'node:crypto';

const origin = 'http://127.0.0.1:8017';
const ready = await (await fetch(origin + '/health/ready')).json();
if (ready.live_calls !== false) throw new Error('Simulator must be enabled');
const credentials = {tenant_id: randomUUID(), login: 'ui-admin', password: randomBytes(24).toString('base64url')};
const created = spawnSync('.venv/bin/python', ['-c', `import json,sys,os; from uuid import UUID; from sqlalchemy.engine import make_url; from ats.cli import bootstrap; from ats.db import session_factory; assert (make_url(os.environ['ATS_DATABASE_URL']).database or '').startswith('ats_smoke'); d=json.load(sys.stdin); bootstrap(session_factory(),UUID(d['tenant_id']),'UI smoke',d['login'],d['password'])`], {input: JSON.stringify(credentials), encoding: 'utf8'});
if (created.status !== 0) throw new Error('Could not create disposable browser account');
await mkdir('artifacts/ui', {recursive: true});
const profile = await mkdtemp(join(tmpdir(), 'ats-ui-'));
const chrome = spawn('/usr/bin/chromium', ['--headless', '--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage', '--remote-debugging-port=0', '--user-data-dir=' + profile, 'about:blank'], {stdio: 'ignore'});
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
let ws;
try {
  let port;
  for (let i = 0; i < 100; i++) {
    try { port = Number((await readFile(join(profile, 'DevToolsActivePort'), 'utf8')).split('\n')[0]); break; } catch { await delay(100); }
  }
  if (!port) throw new Error('Chromium startup timeout');
  const pages = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
  ws = new WebSocket(pages.find(p => p.type === 'page').webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { ws.addEventListener('open', resolve, {once: true}); ws.addEventListener('error', reject, {once: true}); });
  let sequence = 0;
  const waiting = new Map(); const exceptions = [];
  ws.addEventListener('message', event => { const message = JSON.parse(event.data); if (message.id) { const callback = waiting.get(message.id); waiting.delete(message.id); callback?.(message); } else if (message.method === 'Runtime.exceptionThrown') exceptions.push(message.params.exceptionDetails.text); });
  const cdp = (method, params = {}) => new Promise((resolve, reject) => { const id = ++sequence; const timer = setTimeout(() => { waiting.delete(id); reject(new Error('CDP timeout: ' + method)); }, 15000); waiting.set(id, result => { clearTimeout(timer); result.error ? reject(new Error(result.error.message)) : resolve(result.result); }); ws.send(JSON.stringify({id, method, params})); });
  const evaluate = async expression => { const result = await cdp('Runtime.evaluate', {expression, awaitPromise: true, returnByValue: true}); if (result.exceptionDetails) throw new Error('Browser expression failed'); return result.result.value; };
  const wait = async (expression, label) => { for (let i = 0; i < 100; i++) { if (await evaluate(expression)) return; await delay(100); } throw new Error('Browser timeout: ' + label); };
  const click = label => evaluate(`Array.from(document.querySelectorAll('button')).find(b=>b.textContent.trim()===${JSON.stringify(label)})?.click()`);
  const fill = (selector, value) => evaluate(`(()=>{const input=document.querySelector(${JSON.stringify(selector)});if(!input)throw Error('missing input'); Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,${JSON.stringify(value)});input.dispatchEvent(new Event('input',{bubbles:true}));})()`);
  const screenshot = async name => { const result = await cdp('Page.captureScreenshot', {format: 'png', captureBeyondViewport: false}); await writeFile(`artifacts/ui/${name}.png`, Buffer.from(result.data, 'base64')); };
  await cdp('Runtime.enable'); await cdp('Page.enable');
  await cdp('Emulation.setDeviceMetricsOverride', {width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false});
  await cdp('Page.navigate', {url: origin});
  await wait(`!!document.querySelector('input[name="tenant_id"]')`, 'login screen');
  for (const [key, value] of Object.entries(credentials)) await fill(`input[name="${key}"]`, value);
  await evaluate(`document.querySelector('form').requestSubmit()`);
  await wait(`document.querySelector('aside .identity b')?.textContent==='ui-admin'`, 'login');
  await click('Контакты и базы');
  await wait(`!!document.querySelector('input[name="phone"]')`, 'contact form');
  await fill('input[name="name"]', 'UI smoke contact'); await fill('input[name="phone"]', '+79990000666'); await fill('input[name="consent_source"]', 'Synthetic browser consent');
  await evaluate(`document.querySelector('input[name="consent"]').click()`);
  await evaluate(`document.querySelector('input[name="phone"]').closest('form').requestSubmit()`);
  await wait(`document.body.innerText.includes('Контакт добавлен.') && !!Array.from(document.querySelectorAll('td')).find(td=>td.textContent==='+79990000666')`, 'saved contact');
  await screenshot('contacts');
  await click('Кампании');
  await wait(`!!Array.from(document.querySelectorAll('fieldset')).find(f=>f.querySelector('legend')?.textContent.includes('Контакты с согласием'))?.querySelector('input')`, 'campaign contact');
  await evaluate(`Array.from(document.querySelectorAll('fieldset')).find(f=>f.querySelector('legend')?.textContent.includes('Контакты с согласием')).querySelector('input').click()`);
  await fill('input[name="name"]', 'UI smoke campaign');
  await evaluate(`document.querySelector('input[name="name"]').closest('form').requestSubmit()`);
  await wait(`Array.from(document.querySelectorAll('tr')).some(r=>r.textContent.includes('UI smoke campaign'))`, 'saved campaign');
  await click('Запустить');
  await wait(`fetch('/api/v1/workspace/calls').then(r=>r.json()).then(r=>r.items.some(c=>c.status==='completed'))`, 'simulator result');
  await screenshot('campaigns');
  await click('Журнал звонков');
  await wait(`Array.from(document.querySelectorAll('td')).some(td=>td.textContent==='Завершено')`, 'journal');
  await click('Подробности');
  await wait(`document.body.innerText.includes('Этапы обработки')`, 'call timeline'); await screenshot('call-detail'); await click('Закрыть');
  const tabs = ['Дашборд','Кампании','Контакты и базы','Журнал звонков','Пул номеров','Операторы и очередь','Сценарии','Чёрный список','Отчёты','Пользователи','Настройки','Логи'];
  for (const tab of tabs) { await click(tab); await wait(`document.querySelector('main header h1')?.textContent.trim()===${JSON.stringify(tab)}`, tab); await delay(120); if (await evaluate(`!!document.querySelector('[role="alert"]')`)) throw new Error('Page error: ' + tab); }
  await screenshot('logs');
  if (exceptions.length) throw new Error('JavaScript exceptions: ' + exceptions.join(', '));
  const report = {screens: tabs, browser: 'Chromium', contact: 'saved', campaign: 'simulated_completed', timeline: 'visible', javascript_errors: exceptions.length, live_telephony_tested: false};
  await writeFile('artifacts/ui-smoke.json', JSON.stringify(report, null, 2) + '\n');
  process.stdout.write(JSON.stringify(report) + '\n');
} finally {
  ws?.close(); chrome.kill('SIGTERM');
  if (chrome.exitCode === null) await new Promise(resolve => chrome.once('exit', resolve));
  await rm(profile, {recursive: true, force: true});
}
