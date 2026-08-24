<script>
  let { history = [], onOpen = null } = $props();

  function short(value) {
    return value ? `${value.slice(0, 4)}...${value.slice(-4)}` : '—';
  }

  function scannedAt(value) {
    return value ? new Date(value).toLocaleString() : '—';
  }
</script>

<section>
  <header><b>TARGET HISTORY</b><span>{history.length} SAVED</span></header>
  <div class="list">
    {#each history as item (item.query)}
      <button onclick={() => onOpen?.(item)} title={item.query}>
        <strong>{item.token_symbol || short(item.query)}</strong>
        <small>{item.launch_count + item.linked_launch_count} launches · {item.scan_count} scans</small>
        <time>{scannedAt(item.last_scanned_at)}</time>
      </button>
    {:else}
      <p>No target scanned yet.</p>
    {/each}
  </div>
</section>

<style>
  section { margin-bottom: 8px; background: var(--surface-container-lowest); border: 1.5px solid var(--outline-variant); }
  header { display: flex; justify-content: space-between; padding: 6px 10px; background: var(--surface-container-low); border-bottom: 1px solid var(--outline-variant); color: var(--stark-white); font: 800 10px var(--font-headline); }
  header span { color: var(--on-surface-variant); font: 700 9px var(--font-meta); }
  .list { display: flex; flex-direction: column; max-height: 240px; overflow-y: auto; }
  button { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 2px 7px; padding: 6px 8px; text-align: left; background: transparent; border: 0; border-bottom: 1px solid var(--outline-variant); color: var(--on-surface); cursor: pointer; }
  button:hover { background: var(--surface-container-high); }
  strong { color: var(--tertiary); font: 800 11px var(--font-data); }
  small, time { color: var(--on-surface-variant); font: 9px var(--font-meta); }
  small { grid-column: 1 / -1; }
  time { grid-column: 2; grid-row: 1; text-align: right; white-space: nowrap; }
  p { margin: 0; padding: 8px; color: var(--on-surface-variant); font: 10px var(--font-data); }
</style>
