import { useCallback, useEffect, useRef } from 'react';
import { invalidateApiCache } from './api';

const LIVE_TABS = new Set(['dashboard', 'campaigns', 'journal', 'acd', 'reports']);
const POLL_INTERVAL_MS = 15000;
const EVENT_COALESCE_MS = 800;

export function useLiveUpdates(enabled, activeTab, onRefresh) {
  const enabledRef = useRef(enabled);
  const tabRef = useRef(activeTab);
  const refreshRef = useRef(onRefresh);
  const pendingEventRef = useRef(null);
  enabledRef.current = enabled;
  tabRef.current = activeTab;
  refreshRef.current = onRefresh;

  const refresh = useCallback(() => {
    if (!enabledRef.current || document.hidden || !LIVE_TABS.has(tabRef.current)) return;
    invalidateApiCache();
    refreshRef.current();
  }, []);

  const notify = useCallback(() => {
    if (!enabledRef.current) return;
    invalidateApiCache();
    if (document.hidden || !LIVE_TABS.has(tabRef.current) || pendingEventRef.current !== null) return;
    pendingEventRef.current = window.setTimeout(() => {
      pendingEventRef.current = null;
      refresh();
    }, EVENT_COALESCE_MS);
  }, [refresh]);

  useEffect(() => {
    if (!enabled || !LIVE_TABS.has(activeTab)) return undefined;
    const interval = window.setInterval(refresh, POLL_INTERVAL_MS);
    const onVisibility = () => { if (!document.hidden) refresh(); };
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      window.clearInterval(interval);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [enabled, activeTab, refresh]);

  useEffect(() => () => {
    if (pendingEventRef.current !== null) window.clearTimeout(pendingEventRef.current);
  }, []);

  return notify;
}
