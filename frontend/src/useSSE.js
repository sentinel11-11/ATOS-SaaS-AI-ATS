import { useEffect, useRef } from 'react';

// A single connection per authenticated tab. Callback changes do not reconnect.
export function useSSE(onEvent, enabled = true) {
  const callback = useRef(onEvent);
  callback.current = onEvent;

  useEffect(() => {
    if (!enabled) return undefined;
    let source;
    let timer;
    let stopped = false;
    let failures = 0;
    const close = () => { if (source) source.close(); };

    function connect() {
      if (stopped || document.hidden) return;
      source = new EventSource('/api/v2/events');
      source.onopen = () => { failures = 0; };
      ['call', 'item', 'acd', 'campaign', 'agent', 'refresh'].forEach((type) => {
        source.addEventListener(type, (event) => {
          try { callback.current?.(type, JSON.parse(event.data)); }
          catch { /* An invalid event must not break subsequent updates. */ }
        });
      });
      source.onerror = () => {
        close();
        if (!stopped) timer = setTimeout(connect, Math.min(30000, 1000 * 2 ** failures++));
      };
    }

    const visibility = () => {
      close();
      clearTimeout(timer);
      if (!document.hidden) connect();
    };
    document.addEventListener('visibilitychange', visibility);
    connect();
    return () => {
      stopped = true;
      clearTimeout(timer);
      close();
      document.removeEventListener('visibilitychange', visibility);
    };
  }, [enabled]);
}
