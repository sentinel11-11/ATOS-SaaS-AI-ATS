import React from 'react';
import { act, cleanup, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { invalidateApiCache } from './api';
import { useLiveUpdates } from './useLiveUpdates';

vi.mock('./api', () => ({ invalidateApiCache: vi.fn() }));

const setHidden = (value) => {
  Object.defineProperty(document, 'hidden', { configurable: true, value });
};

beforeEach(() => {
  vi.useFakeTimers();
  vi.clearAllMocks();
  setHidden(false);
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

it('coalesces SSE bursts and invalidates cached reads for the visible report', () => {
  const refresh = vi.fn();
  const { result } = renderHook(() => useLiveUpdates(true, 'reports', refresh));
  act(() => { result.current(); result.current(); result.current(); });
  expect(refresh).not.toHaveBeenCalled();
  act(() => { vi.advanceTimersByTime(800); });
  expect(refresh).toHaveBeenCalledTimes(1);
  expect(invalidateApiCache).toHaveBeenCalled();
});

it('polls every 15 seconds, pauses when hidden and reconciles on return', () => {
  const refresh = vi.fn();
  renderHook(() => useLiveUpdates(true, 'journal', refresh));
  act(() => { vi.advanceTimersByTime(15000); });
  expect(refresh).toHaveBeenCalledTimes(1);
  setHidden(true);
  act(() => { vi.advanceTimersByTime(30000); });
  expect(refresh).toHaveBeenCalledTimes(1);
  setHidden(false);
  act(() => { document.dispatchEvent(new Event('visibilitychange')); });
  expect(refresh).toHaveBeenCalledTimes(2);
});

it('does not replace editable page state when background changes arrive', () => {
  const refresh = vi.fn();
  const { result } = renderHook(() => useLiveUpdates(true, 'contacts', refresh));
  act(() => { result.current(); vi.advanceTimersByTime(30000); });
  expect(invalidateApiCache).toHaveBeenCalledTimes(1);
  expect(refresh).not.toHaveBeenCalled();
});

it('stops refreshing when disabled and cleans up scheduled work on unmount', () => {
  const refresh = vi.fn();
  const { result, rerender, unmount } = renderHook(({ enabled }) => useLiveUpdates(enabled, 'dashboard', refresh), { initialProps: { enabled: true } });
  act(() => { result.current(); });
  rerender({ enabled: false });
  act(() => { vi.advanceTimersByTime(30000); });
  expect(refresh).not.toHaveBeenCalled();
  unmount();
});
