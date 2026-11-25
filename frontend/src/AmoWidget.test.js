import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {afterEach, beforeEach, expect, it, vi} from 'vitest';

const source = readFileSync(resolve('../amo-widget/script.js'), 'utf8');
let widget;
let listeners;
let windowListeners;

function deferred(promise) {
  const result = {done(fn) {promise.then(fn, () => {}); return result;}, fail(fn) {promise.catch(fn); return result;}};
  return result;
}

async function flush() {await vi.advanceTimersByTimeAsync(1);}

function mount(handler, owner=identity) {
  let Constructor;
  const signed = vi.fn(options => deferred(Promise.resolve().then(() =>
    pathOf(options).endsWith('/context') ? {responsible: owner} : handler(options))));
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

beforeEach(() => {vi.useFakeTimers(); listeners = vi.spyOn(document, 'addEventListener'); windowListeners=vi.spyOn(window,'addEventListener');});
afterEach(() => {
  if (widget) widget.callbacks.destroy();
  for (const [event, listener] of listeners.mock.calls) if (['click','change'].includes(event)) document.removeEventListener(event, listener, false);
  for (const [event, listener] of windowListeners.mock.calls) if (['pagehide','pageshow'].includes(event)) window.removeEventListener(event, listener);
  vi.clearAllTimers(); vi.useRealTimers(); vi.restoreAllMocks(); document.body.innerHTML = '';
  delete window.RATSJsSIP;
  delete window.RATSPhoneWidget;
});

const identity = {operator_name: 'Manager', operator_id: 'operator', status: 'free',
  forward_number_masked: '+7***1000', forward_available: true, browser_online: true};
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

it('lets the participant cancel their active call from another computer and waits for confirmed termination', async () => {
  let cancelled=false, ended=false;
  const signed=mount(options => {
    if(pathOf(options).endsWith('/me'))return {...identity,active_call:{call_id:'owned-call',state:'reconciliation_required'}};
    if(pathOf(options).endsWith('/hangup')){cancelled=true;return {ok:true,state:'ending'};}
    return {call_id:'owned-call',state:ended?'ended':'reconciliation_required',cancel_requested:cancelled};
  });
  await flush();
  expect(document.querySelector('.rats-hangup').disabled).toBe(false);
  document.querySelector('.rats-hangup').click();await flush();
  expect(signed.mock.calls.some(([o])=>pathOf(o).endsWith('/owned-call/hangup'))).toBe(true);
  await vi.advanceTimersByTimeAsync(1800);
  expect(document.querySelector('.rats-call-state').textContent).toBe('Завершаем');
  expect(document.querySelector('.rats-start').disabled).toBe(true);
  ended=true;await vi.advanceTimersByTimeAsync(1800);
  expect(document.querySelector('.rats-call-state').textContent).toBe('Завершён');
  expect(document.querySelector('.rats-hangup').disabled).toBe(true);
});

it('double click creates and starts one request; no keypress is required', async () => {
  const signed = mount(options => pathOf(options).endsWith('/me') ? identity : {call_id: 'call-one', state: 'queued', operator_call_token: 'nonce'});
  await flush();
  document.querySelector('.rats-mode-forward').click();
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

it('honors forwarding selected without a mouse click and keeps it after refresh', async () => {
  const mic=vi.fn();
  Object.defineProperty(navigator,'mediaDevices',{configurable:true,value:{getUserMedia:mic}});
  const signed=mount(options=>pathOf(options).endsWith('/me')?identity:{call_id:'forward-call',state:'queued'});
  await flush();
  const forward=document.querySelector('.rats-mode-forward');
  forward.checked=true;forward.dispatchEvent(new Event('change',{bubbles:true}));
  await vi.advanceTimersByTimeAsync(1800);
  expect(forward.checked).toBe(true);
  document.querySelector('.rats-start').click();await flush();
  const command=signed.mock.calls.find(([o])=>pathOf(o).endsWith('/calls'))[0];
  expect(JSON.parse(command.data).mode).toBe('forward');
  expect(mic).not.toHaveBeenCalled();
});

it('uses the card responsible and starts their browser without requesting the initiators microphone', async () => {
  const getUserMedia = vi.fn();
  Object.defineProperty(navigator, 'mediaDevices', {configurable: true, value: {getUserMedia}});
  const owner = {...identity, operator_id: 'responsible', operator_name: 'Renat', forward_number_masked: '+7***2000'};
  const signed = mount(options => pathOf(options).endsWith('/me') ? identity :
    {call_id: 'remote-call', state: 'queued', is_participant: false}, owner);
  await flush();
  expect(document.querySelector('.rats-user').textContent).toBe('Renat');
  expect(document.querySelector('.rats-forward').textContent).toBe('+7***2000');
  document.querySelector('.rats-start').click();await flush();
  expect(getUserMedia).not.toHaveBeenCalled();
  const command = signed.mock.calls.find(([value]) => pathOf(value).endsWith('/calls'))[0];
  expect(JSON.parse(command.data)).toMatchObject({mode: 'browser', protocol_version: 3});
  expect(JSON.parse(command.data).operator_id).toBeUndefined();
});

it('refreshes the responsible and forwarding number while the same card stays open', async () => {
  const owner = {...identity, operator_name: 'Ilya'};
  mount(() => identity, owner);await flush();
  expect(document.querySelector('.rats-user').textContent).toBe('Ilya');
  owner.operator_name='Renat';owner.operator_id='responsible';owner.forward_number_masked='+7***2000';
  await vi.advanceTimersByTimeAsync(5200);
  expect(document.querySelector('.rats-user').textContent).toBe('Renat');
  expect(document.querySelector('.rats-forward').textContent).toBe('+7***2000');
});

it('keeps the receiving phone through card destruction and reconnects after browser back navigation', async () => {
  const agents=[];
  class UA {
    constructor(){this.events={};this.stop=vi.fn();agents.push(this);}
    on(name, fn){this.events[name]=fn;}
    start(){this.events.registered();}
  }
  window.RATSJsSIP={UA,WebSocketInterface:class {}};
  mount(options => {
    if(pathOf(options).endsWith('/me'))return {...identity,browser_available:true};
    if(pathOf(options).endsWith('/webrtc/config'))return {ready:true,wss_url:'wss://ats.example.invalid/ws',sip_uri:'sip:manager@ats.example.invalid',sip_password:'fixture-only'};
    return {ok:true};
  });
  await flush();expect(agents).toHaveLength(1);
  widget.callbacks.destroy();expect(agents[0].stop).not.toHaveBeenCalled();
  window.dispatchEvent(new Event('pagehide'));expect(agents[0].stop).toHaveBeenCalledOnce();
  const event=new Event('pageshow');Object.defineProperty(event,'persisted',{value:true});
  window.dispatchEvent(event);await flush();expect(agents).toHaveLength(2);
});

it('retries an ambiguous network failure with the same request ID', async () => {
  let attempts = 0;
  const signed = mount(options => {
    if (pathOf(options).endsWith('/me')) return identity;
    if (pathOf(options).endsWith('/calls') && ++attempts === 1) throw {status: 503, responseJSON: {error: 'connection_failed'}};
    return {call_id: 'call-one', state: 'queued', operator_call_token: 'nonce'};
  });
  await flush(); document.querySelector('.rats-mode-forward').click(); document.querySelector('.rats-start').click(); await flush();
  document.querySelector('.rats-start').click(); await flush();
  const commands = signed.mock.calls.filter(([value]) => pathOf(value).endsWith('/calls')).map(([value]) => JSON.parse(value.data));
  expect(commands).toHaveLength(2);
  expect(commands[0].client_request_id).toBe(commands[1].client_request_id);
});

it('can resume an ambiguous request after navigating to another card without changing its destination or request ID', async () => {
  let tries=0;
  const signed=mount(options=>{
    if(pathOf(options).endsWith('/me'))return identity;
    if(pathOf(options).endsWith('/calls')&&++tries===1)throw {status:0};
    return {call_id:'original-call',state:'queued'};
  });
  await flush();document.querySelector('.rats-mode-forward').click();
  document.querySelector('.rats-start').click();await flush();
  widget.system=()=>({area:'lcard',entity_id:202,subdomain:'omkr'});
  await vi.advanceTimersByTimeAsync(5200);
  expect(document.querySelector('.rats-retry').hidden).toBe(false);
  document.querySelector('.rats-retry').click();await flush();
  const requests=signed.mock.calls.filter(([o])=>pathOf(o).endsWith('/calls')).map(([o])=>JSON.parse(o.data));
  expect(requests).toHaveLength(2);expect(requests[0]).toEqual(requests[1]);
  expect(requests[1].entity_id).toBe(101);
});

it('resolves a clicked phone through card membership even when the SDK supplies a different contact ID', async () => {
  const signed=mount(options=>pathOf(options).endsWith('/me')?identity:{call_id:'verified',state:'queued'});
  await flush();document.querySelector('.rats-mode-forward').click();
  widget.add_action.mock.calls[0][1]({contact_id:999,value:'+79990000101'});await flush();
  const command=JSON.parse(signed.mock.calls.find(([o])=>pathOf(o).endsWith('/calls'))[0].data);
  expect(command.crm_contact_id).toBe(123);
});

it('shares pending SIP initialization and serializes slow heartbeats', async () => {
  const agents=[];let leased,beat;
  class UA{
    constructor(){this.events={};agents.push(this);}
    on(name,fn){this.events[name]=fn;}
    start(){}
    stop(){}
  }
  window.RATSJsSIP={UA,WebSocketInterface:class{}};
  const signed=mount(options=>{
    const path=pathOf(options);
    if(path.endsWith('/me'))return {...identity,browser_available:true};
    if(path.endsWith('/agent/lease'))return new Promise(resolve=>{leased=resolve;});
    if(path.endsWith('/webrtc/config'))return {ready:true,wss_url:'wss://ats.example.invalid/ws',sip_uri:'sip:manager@ats.example.invalid',sip_password:'fixture'};
    if(path.endsWith('/heartbeat'))return new Promise(resolve=>{beat=resolve;});
    return {ok:true};
  });
  await flush();widget.callbacks.init();await flush();
  expect(signed.mock.calls.filter(([o])=>pathOf(o).endsWith('/agent/lease'))).toHaveLength(1);
  leased({ok:true});await flush();expect(agents).toHaveLength(1);
  expect(document.querySelector('.rats-agent-status').textContent).not.toContain('зарегистрирован');
  agents[0].events.registered();await flush();
  await vi.advanceTimersByTimeAsync(9000);
  expect(signed.mock.calls.filter(([o])=>pathOf(o).endsWith('/heartbeat'))).toHaveLength(1);
  beat({status:'free'});await flush();
  await vi.advanceTimersByTimeAsync(3500);
  expect(signed.mock.calls.filter(([o])=>pathOf(o).endsWith('/heartbeat'))).toHaveLength(2);
});

it('ignores a late heartbeat rejection belonging to the page before back navigation', async () => {
  const agents=[],beats=[];
  class UA{
    constructor(){this.events={};this.stop=vi.fn();agents.push(this);}
    on(name,fn){this.events[name]=fn;}
    start(){this.events.registered();}
  }
  window.RATSJsSIP={UA,WebSocketInterface:class{}};
  mount(options=>{
    const path=pathOf(options);
    if(path.endsWith('/me'))return {...identity,browser_available:true};
    if(path.endsWith('/webrtc/config'))return {ready:true,wss_url:'wss://ats.example.invalid/ws',sip_uri:'sip:manager@ats.example.invalid',sip_password:'fixture'};
    if(path.endsWith('/heartbeat'))return new Promise((resolve,reject)=>beats.push({resolve,reject}));
    return {ok:true};
  });
  await flush();expect(beats).toHaveLength(1);
  window.dispatchEvent(new Event('pagehide'));
  const event=new Event('pageshow');Object.defineProperty(event,'persisted',{value:true});
  window.dispatchEvent(event);await flush();
  expect(agents).toHaveLength(2);expect(beats).toHaveLength(2);
  beats[0].reject({status:409,responseJSON:{error:'agent_session_lost'}});await flush();
  expect(agents[1].stop).not.toHaveBeenCalled();
  expect(document.querySelector('.rats-agent-status').textContent).toContain('зарегистрирован');
  beats[1].resolve({status:'free'});await flush();
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
    if (pathOf(options).endsWith('/me')) return {...identity, browser_available: true};
    if (pathOf(options).endsWith('/webrtc/config')) return {ready: true, wss_url: 'wss://ats.example.invalid/asterisk/ws', sip_uri: 'sip:manager@ats.example.invalid', sip_password: 'fixture-only'};
    return {call_id: 'browser-call', operator_call_token: 'a'.repeat(48), state: serverState,
      is_participant: true, context: {contact_name: 'Client'}};
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
  ua.events.newRTCSession({originator: 'remote', session, request: {getHeader: () => 'a'.repeat(48)}});
  await flush();
  expect(session.answer).toHaveBeenCalledOnce();
  const actualEvents={};
  const actual={connectionState:'new',addEventListener:(name,fn)=>{actualEvents[name]=fn;}};
  handlers.peerconnection({peerconnection:actual});
  handlers.confirmed(); await flush();
  expect(document.querySelector('.rats-call-state').textContent).not.toBe('Разговор');
  expect(signed.mock.calls.some(([value]) => pathOf(value).endsWith('/ready'))).toBe(false);
  pc.connectionState='connected';pcEvents.connectionstatechange();await flush();
  expect(signed.mock.calls.some(([value]) => pathOf(value).endsWith('/ready'))).toBe(false);
  actual.connectionState='connected';actualEvents.connectionstatechange();await flush();
  expect(signed.mock.calls.some(([value]) => pathOf(value).endsWith('/ready'))).toBe(true);
  serverState = 'connected'; await vi.advanceTimersByTimeAsync(1800);
  expect(document.querySelector('.rats-call-state').textContent).toBe('Разговор');
  expect(document.querySelector('#rats-widget-root .rats-agent-host #rats-browser-agent')).not.toBeNull();
  expect(document.querySelector('.rats-agent-hangup').hidden).toBe(true);
});
