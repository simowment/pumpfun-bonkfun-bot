<script>
  import { onMount } from 'svelte';

  import Header from './lib/components/Header.svelte';
  import KpiStrip from './lib/components/KpiStrip.svelte';
  import TargetsPanel from './lib/components/TargetsPanel.svelte';
  import PositionsPanel from './lib/components/PositionsPanel.svelte';
  import EventFeed from './lib/components/EventFeed.svelte';
  import CommandRail from './lib/components/CommandRail.svelte';

  import { API_BASE, WS_URL, fetchHealth, fetchState } from './lib/api.js';
  import { describeEvent } from './lib/format.js';

  const MAX_FEED_EVENTS = 200;
  const HEALTH_POLL_MS = 8000;
  const RECONNECT_DELAY_S = 3;

  /** Full API state object; reassigned wholesale on every state message. */
  let state = $state.raw(null);

  /** Display rows derived from raw websocket events, newest first. */
  let events = $state([]);

  /** Connection lifecycle: 'connecting' | 'online' | 'offline'. */
  let conn = $state({ status: 'connecting', detail: '', retryLeft: 0 });

  let lastStateAt = $state(null);
  let now = $state(new Date());

  let wsRef = null;
  let reconnectTimer = null;
  let healthTimer = null;
  let clockTimer = null;

  /* --- Derived view model -------------------------------------------------- */

  const kpis = $derived.by(() => {
    const s = state ?? {};
    const wallets = Array.isArray(s.wallets) ? s.wallets : [];
    const activeWallets = wallets.filter(
      (w) => w.status && w.status !== 'expired' && w.status !== 'ignored',
    );
    return [
      {
        key: 'funders',
        label: 'Tracked Funders',
        value: Array.isArray(s.funders) ? s.funders.length : 0,
        tone: 'cyan',
      },
      {
        key: 'wallets',
        label: 'Active Wallets',
        value: activeWallets.length,
        hint: `${wallets.length} total`,
        tone: 'blue',
      },
      {
        key: 'launches',
        label: 'Launches',
        value: Array.isArray(s.launches) ? s.launches.length : 0,
        tone: 'amber',
      },
      {
        key: 'positions',
        label: 'Open Positions',
        value: Array.isArray(s.positions) ? s.positions.length : 0,
        tone: 'acid',
      },
    ];
  });

  const targets = $derived(Array.isArray(state?.targets) ? state.targets : []);
  const positions = $derived(Array.isArray(state?.positions) ? state.positions : []);
  const stage = $derived(state?.snapshot?.stage ?? null);
  const killSwitch = $derived(Boolean(state?.snapshot?.kill_switch_active));
  const hasState = $derived(state !== null);
  const loading = $derived(!hasState && conn.status !== 'offline');

  /* --- Connection lifecycle ------------------------------------------------ */

  function setConn(partial) {
    conn = { ...conn, ...partial };
  }

  async function loadState() {
    try {
      const data = await fetchState();
      state = data;
      lastStateAt = Date.now();
      if (conn.status !== 'online') setConn({ status: 'online', detail: '' });
    } catch (err) {
      if (conn.status !== 'offline') {
        setConn({ status: 'offline', detail: err.message });
      }
    }
  }

  function pushEvent(rawEvent) {
    const item = describeEvent(rawEvent);
    events = [item, ...events].slice(0, MAX_FEED_EVENTS);
  }

  function openSocket() {
    clearTimeout(reconnectTimer);

    let ws;
    try {
      ws = new WebSocket(WS_URL);
    } catch {
      setConn({ status: 'offline', detail: 'websocket unavailable' });
      scheduleReconnect();
      return;
    }

    wsRef = ws;

    ws.onopen = () => {
      setConn({ status: 'online', detail: '' });
    };

    ws.onmessage = (event) => {
      let message = null;
      try {
        message = JSON.parse(event.data);
      } catch {
        return; // non-JSON frames are ignored
      }
      if (message && message.type === 'state' && message.data) {
        state = message.data;
        lastStateAt = Date.now();
      } else if (message && message.type === 'event' && message.data) {
        pushEvent(message.data);
      }
    };

    ws.onclose = () => {
      if (wsRef === ws) {
        setConn({ status: 'offline', detail: 'socket closed' });
        scheduleReconnect();
      }
    };

    ws.onerror = () => {
      // onclose follows; no duplicate handling needed here.
    };
  }

  function scheduleReconnect() {
    clearTimeout(reconnectTimer);
    setConn({ retryLeft: RECONNECT_DELAY_S });
    reconnectTimer = setTimeout(() => {
      reconnectNow();
    }, RECONNECT_DELAY_S * 1000);
  }

  function reconnectNow() {
    clearTimeout(reconnectTimer);
    setConn({ retryLeft: 0 });
    loadState();
    openSocket();
  }

  async function pollHealth() {
    try {
      await fetchHealth();
      if (conn.status === 'offline') reconnectNow();
    } catch {
      if (conn.status === 'online') {
        setConn({ status: 'offline', detail: 'health check failed' });
      }
    }
  }

  /* --- Lifecycle ----------------------------------------------------------- */

  onMount(() => {
    loadState();
    openSocket();
    healthTimer = setInterval(pollHealth, HEALTH_POLL_MS);
    clockTimer = setInterval(() => {
      now = new Date();
      if (conn.retryLeft > 0) setConn({ retryLeft: conn.retryLeft - 1 });
    }, 1000);

    return () => {
      clearTimeout(reconnectTimer);
      clearInterval(healthTimer);
      clearInterval(clockTimer);
      if (wsRef) wsRef.close();
    };
  });
</script>

<div class="app {conn.status === 'offline' ? 'app--offline' : ''}">
  <Header
    {conn}
    {stage}
    {killSwitch}
    {now}
    apiBase={API_BASE}
  />

  {#if conn.status === 'offline'}
    <div class="banner banner-offline" role="status">
      <span>BACKEND OFFLINE — {conn.detail || 'no response'} at {API_BASE}</span>
      {#if conn.retryLeft > 0}
        <span class="mono">auto-reconnect in {conn.retryLeft}s</span>
      {/if}
      <button class="btn btn-accent" onclick={reconnectNow}>RECONNECT NOW</button>
    </div>
  {:else if conn.status === 'connecting'}
    <div class="banner banner-connecting" role="status">
      <span>CONNECTING TO {API_BASE}…</span>
    </div>
  {/if}

  {#if killSwitch}
    <div class="banner banner-kill" role="alert">
      <span>KILL SWITCH ACTIVE — execution halted. Use <code>kill</code> to release.</span>
    </div>
  {/if}

  <KpiStrip {kpis} />

  <main class="dashboard">
    <TargetsPanel {targets} {loading} />
    <EventFeed {events} online={conn.status === 'online'} {loading} />
    <div class="col-right">
      <PositionsPanel {positions} {loading} />
      <CommandRail online={conn.status === 'online'} {positions} />
    </div>
  </main>
</div>