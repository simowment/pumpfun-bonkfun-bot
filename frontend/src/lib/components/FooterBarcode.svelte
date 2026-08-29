<script>
  let {
    version = 'RUGBOT', buildId = 'SVELTE', network = 'MAINNET',
    rpcStatus = 'UNKNOWN', jitoStatus = 'NOT CHECKED', radarStatus = 'UNKNOWN',
  } = $props();
</script>

<footer>
  <div class="brand">|||| ||| || |||| <span>{version} // {buildId} // {network}</span></div>
  <div>[ RUGBOT INTELLIGENCE ENGINE // OBSERVE ONLY ]</div>
  <div class="statuses"><span class:ok={rpcStatus === 'CONNECTED'}>RPC: {rpcStatus}</span><span>JITO: {jitoStatus}</span><span>RADAR: {radarStatus}</span></div>
</footer>

<style>
  footer { display: flex; justify-content: space-between; align-items: center; gap: 8px; flex-wrap: wrap; padding: 6px 12px; background: var(--surface-container-lowest); border: 1.5px solid var(--outline-variant); color: var(--on-surface-variant); font: 700 10px var(--font-meta); }
  .brand { letter-spacing: 2px; } .brand span { margin-left: 8px; letter-spacing: normal; }
  .statuses { display: flex; gap: 10px; } .statuses span::before { content: '■ '; color: var(--outline-variant); } .statuses .ok::before { color: var(--green-ok); }
</style>
