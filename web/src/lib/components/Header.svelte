<script>
  import { formatUtcClock } from '../format.js';

  let { conn, stage, killSwitch, now, apiBase } = $props();

  const stateLabel = $derived(
    conn.status === 'online' ? 'LIVE' : conn.status === 'connecting' ? 'CONNECTING' : 'OFFLINE',
  );
</script>

<header class="ops-header">
  <div class="brand">
    <svg class="brand-mark" viewBox="0 0 32 32" aria-hidden="true">
      <rect width="32" height="32" rx="7" fill="#0b111d" stroke="#2b3a52" stroke-width="1" />
      <circle cx="11" cy="11" r="2.4" fill="#4cc9f0" />
      <circle cx="21" cy="11" r="2.4" fill="#9dff3f" />
      <circle cx="11" cy="21" r="2.4" fill="#9dff3f" />
      <circle cx="21" cy="21" r="2.4" fill="#4cc9f0" />
    </svg>
    <div class="brand-text">
      <div class="brand-name">RUGBOT</div>
      <div class="brand-sub">OPERATIONS CONSOLE</div>
    </div>
  </div>

  <div class="header-status">
    <span class="conn-pill conn-{conn.status}" title="websocket: {apiBase}/api/events">
      <span class="dot"></span>{stateLabel}
    </span>
    <span class="tag">LISTENER <b>PUMPPORTAL</b></span>
    {#if stage}
      <span class="tag tag-stage">STAGE <b>{stage}</b></span>
    {/if}
    {#if killSwitch}
      <span class="tag tag-danger">KILL SWITCH ACTIVE</span>
    {/if}
    <span class="clock" title="api base: {apiBase}">{formatUtcClock(now)} UTC</span>
  </div>
</header>