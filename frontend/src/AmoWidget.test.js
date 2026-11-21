import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {afterEach, beforeEach, expect, it, vi} from 'vitest';

const source = readFileSync(resolve('../amo-widget/script.js'), 'utf8');
let widget;
let listeners;

function deferred(promise) {
  const result = {done(fn) {promise.then(fn, () => {}); return result;}, fail(fn) {promise.catch(fn); return result;}};
  return result;
}

async function flush() {await vi.advanceTimersByTimeAsync(1);}

function mount(handler) {
  let Constructor;
  const signed = vi.fn(options => deferred(Promise.resolve().then(() => handler(options))));
  const jquery = {ajax: options => deferred(Promise.resolve(options.url.includes('/contacts/') ? {
    id: 123, name: 'Client from CRM', custom_fields_values: [{field_code: 'PHONE', values: [{value: '+79990000101'}]}],
  } : {_embedded: {contacts: [{id: 123}]}}))};
  new Function('define', source)((_deps, factory) => {Constructor = factory(jquery);});
  widget = new Constructor();
  widget.system = () => ({area: 'lcard', entity_id: 101, subdomain: 'omkr'});
  widget.get_settings = () => ({ratsUrl: 'https://ats.example.invalid'});
  widget.$authorizedAjax = signed;
  widget.render_template = data => {document.body.innerHTML = data.render;};
  widget.add_action = vi.fn();
  widget.callbacks.render(); widget.callbacks.init();
  return signed;
}

beforeEach(() => {vi.useFakeTimers(); listeners = vi.spyOn(document, 'addEventListener');});
afterEach(() => {
  if (widget) widget.callbacks.destroy();
  for (const [event, listener] of listeners.mock.calls) if (event === 'click') document.removeEventListener(event, listener, false);
  vi.clearAllTimers(); vi.useRealTimers(); vi.restoreAllMocks(); document.body.innerHTML = '';
  delete window.RATSJsSIP;
});

const identity = {operator_name: 'Manager', operator_id: 'operator', forward_number_masked: '+7***1000'};
const pathOf = options => new URL(options.url).pathname;

it('keeps calling disabled until the signed identity has arrived', async () => {
  let identify;
  const signed = mount(() => new Promise(resolve => {identify = resolve;}));
  await flush();
  expect(document.querySelector('.rats-start').disabled).toBe(true);
  identify(identity); await flush();
  expect(document.querySelector('.rats-start').disabled).toBe(false);
  expect(signed.mock.calls[0][0].xhrFields.withCredentials).toBe(false);
  expect(document.querySelector('.rats-user').textContent).toBe('Manager');
});

it('double click creates and starts one request; no keypress is required', async () => {
  const signed = mount(options => pathOf(options).endsWith('/me') ? identity : {call_id: 'call-one', state: 'queued', operator_call_token: 'nonce'});
  await flush();
  document.querySelector('.rats-start').click(); document.querySelector('.rats-start').click();
  await flush();
  const calls = signed.mock.calls.map(([value]) => value);
  expect(calls.filter(value => pathOf(value).endsWith('/calls'))).toHaveLength(1);
  expect(calls.filter(value => pathOf(value).endsWith('/start'))).toHaveLength(1);
  const command = JSON.parse(calls.find(value => pathOf(value).endsWith('/calls')).data);
  expect(command.mode).toBe('forward');
  expect(command.crm_contact_id).toBe(123);
  expect(command.amo_user_id).toBeUndefined();
  expect(command.operator_id).toBeUndefined();
});

it('retries an ambiguous network failure with the same request ID', async () => {
  let attempts = 0;
  const signed = mount(options => {
    if (pathOf(options).endsWith('/me')) return identity;
    if (pathOf(options).endsWith('/calls') && ++attempts === 1) throw {status: 503, responseJSON: {error: 'connection_failed'}};
    return {call_id: 'call-one', state: 'queued', operator_call_token: 'nonce'};
  });
  await flush(); document.querySelector('.rats-start').click(); await flush();
  document.querySelector('.rats-start').click(); await flush();
  const commands = signed.mock.calls.filter(([value]) => pathOf(value).endsWith('/calls')).map(([value]) => JSON.parse(value.data));
  expect(commands).toHaveLength(2);
  expect(commands[0].client_request_id).toBe(commands[1].client_request_id);
});

it('rejects an unrelated SIP invite and waits for the real bridge before showing a conversation', async () => {
  let ua;
  class UA {
    constructor() {this.events = {}; ua = this;}
    on(name, fn) {this.events[name] = fn;}
    start() {this.events.registered();}
    stop() {}
  }
  window.RATSJsSIP = {UA, WebSocketInterface: class {}};
  Object.defineProperty(navigator, 'mediaDevices', {configurable: true, value: {getUserMedia: vi.fn(async () => ({active: true, getTracks: () => [{stop() {}}]}))}});
  let serverState = 'ringing_operator';
  const signed = mount(options => {
    if (pathOf(options).endsWith('/me')) return identity;
    if (pathOf(options).endsWith('/webrtc/config')) return {ready: true, wss_url: 'wss://ats.example.invalid/asterisk/ws', sip_uri: 'sip:manager@ats.example.invalid', sip_password: 'fixture-only'};
    return {call_id: 'browser-call', operator_call_token: 'expected-nonce', state: serverState};
  });
  await flush(); document.querySelector('.rats-mode-browser').click(); await flush();
  document.querySelector('.rats-start').click(); await flush();
  const bad = {terminate: vi.fn()};
  ua.events.newRTCSession({originator: 'remote', session: bad, request: {getHeader: () => 'unrelated'}});
  expect(bad.terminate).toHaveBeenCalledOnce();
  const handlers = {};
  const pcEvents = {};
  const pc = {connectionState: 'new', addEventListener: (name, fn) => {pcEvents[name] = fn;}};
  const session = {connection: pc, terminate: vi.fn(), answer: vi.fn(), on: (name, fn) => {handlers[name] = fn;}};
  ua.events.newRTCSession({originator: 'remote', session, request: {getHeader: () => 'expected-nonce'}});
  expect(session.answer).toHaveBeenCalledOnce();
  handlers.confirmed(); await flush();
  expect(document.querySelector('.rats-call-state').textContent).not.toBe('Разговор');
  expect(signed.mock.calls.some(([value]) => pathOf(value).endsWith('/ready'))).toBe(false);
  pc.connectionState = 'connected'; pcEvents.connectionstatechange(); await flush();
  expect(signed.mock.calls.some(([value]) => pathOf(value).endsWith('/ready'))).toBe(true);
  serverState = 'connected'; await vi.advanceTimersByTimeAsync(1800);
  expect(document.querySelector('.rats-call-state').textContent).toBe('Разговор');
});
