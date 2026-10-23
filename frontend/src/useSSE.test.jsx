import React from 'react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, renderHook } from '@testing-library/react';
import { useSSE } from './useSSE';

let instances;
beforeEach(() => {
  instances = [];
  Object.defineProperty(document, 'hidden', { configurable: true, value: false });
  vi.stubGlobal('EventSource', class {
    listeners = {};
    close = vi.fn();
    constructor(url) { this.url = url; instances.push(this); }
    addEventListener(type, handler) { this.listeners[type] = handler; }
  });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it('uses one cookie-authenticated connection while event callback changes', () => {
  const first = vi.fn();
  const second = vi.fn();
  const hook = renderHook(({ handler }) => useSSE(handler, true), { initialProps: { handler: first } });
  expect(instances).toHaveLength(1);
  expect(instances[0].url).toBe('/api/v2/events');
  hook.rerender({ handler: second });
  expect(instances).toHaveLength(1);
  instances[0].listeners.call({ data: JSON.stringify({ id: 'call-id' }) });
  expect(second).toHaveBeenCalledWith('call', { id: 'call-id' });
  expect(first).not.toHaveBeenCalled();
  hook.unmount();
  expect(instances[0].close).toHaveBeenCalled();
});

it('does not connect before authentication and closes when disabled', () => {
  const hook = renderHook(({ enabled }) => useSSE(() => {}, enabled), { initialProps: { enabled: false } });
  expect(instances).toHaveLength(0);
  hook.rerender({ enabled: true });
  expect(instances).toHaveLength(1);
  hook.rerender({ enabled: false });
  expect(instances[0].close).toHaveBeenCalled();
});
