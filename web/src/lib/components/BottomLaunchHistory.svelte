<script>
  import { onMount } from 'svelte';

  let { tokens = [] } = $props();
  const SORT_STORAGE_KEY = 'rugbot.token-history-order';
  const fields = ['symbol', 'name', 'mint', 'creator', 'launch_time', 'status'];
  let sortOrder = $state('newest');
  const creationCount = $derived(tokens.filter((token) => token.isCreationEvent).length);
  const resolvedTargetCount = $derived(tokens.length - creationCount);
  const sortedTokens = $derived.by(() =>
    [...tokens].sort((left, right) => {
      const leftTime = Date.parse(left.launch_time || '');
      const rightTime = Date.parse(right.launch_time || '');
      if (Number.isNaN(leftTime)) return Number.isNaN(rightTime) ? left.mint.localeCompare(right.mint) : 1;
      if (Number.isNaN(rightTime)) return -1;
      return sortOrder === 'newest' ? rightTime - leftTime : leftTime - rightTime;
    }),
  );

  function short(address) {
    return address ? `${address.slice(0, 4)}...${address.slice(-4)}` : '—';
  }

  function createdAt(value) {
    return value ? `${value.slice(0, 16).replace('T', ' ')} UTC` : '—';
  }

  function exportCsv() {
    if (!tokens.length) return;
    const escape = (value) => `"${String(value ?? '').replaceAll('"', '""')}"`;
    const csv = [
      fields.join(','),
      ...sortedTokens.map((token) => fields.map((field) => escape(token[field])).join(',')),
    ].join('\n');
    const url = URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8' }));
    const link = document.createElement('a');
    link.href = url;
    link.download = `finalized_launches_${Date.now()}.csv`;
    link.click();
    URL.revokeObjectURL(url);
  }

  function updateSort(event) {
    sortOrder = event.currentTarget.value;
    localStorage.setItem(SORT_STORAGE_KEY, sortOrder);
  }

  onMount(() => {
    const savedOrder = localStorage.getItem(SORT_STORAGE_KEY);
    if (savedOrder === 'newest' || savedOrder === 'oldest') sortOrder = savedOrder;
  });
</script>

<div class="card">
  <header>
    <span>[ TOKEN TARGET &amp; FINALIZED HISTORY ]</span>
    <div class="settings">
      <label for="history-order">ORDER</label>
      <select id="history-order" value={sortOrder} onchange={updateSort}>
        <option value="newest">NEWEST FIRST</option>
        <option value="oldest">OLDEST FIRST</option>
      </select>
      <button onclick={exportCsv} disabled={!tokens.length}>⤓ EXPORT CSV</button>
    </div>
  </header>
  <div class="scroll">
    <table>
      <thead><tr><th>TOKEN</th><th>MINT</th><th>OPEN</th><th>CREATOR</th><th>CREATED</th><th>STATUS</th></tr></thead>
      <tbody>
        {#each sortedTokens as token (token.mint)}
          <tr>
            <td><strong>{token.symbol || '—'}</strong><small>{token.name || ''}</small></td>
            <td>{short(token.mint)}</td>
            <td class="links">
              {#if token.links?.axiom}<a href={token.links.axiom} target="_blank" rel="noreferrer">AXIOM ↗</a>{/if}
              <a href={token.links?.solscan} target="_blank" rel="noreferrer">SOLSCAN ↗</a>
            </td>
            <td>{short(token.creator)}</td><td class="created">{createdAt(token.launch_time)}</td>
            <td><b class="status">{token.status || 'OBSERVED'}</b></td>
          </tr>
        {:else}
          <tr><td colspan="6" class="empty">No resolved token or finalized launch loaded.</td></tr>
        {/each}
      </tbody>
    </table>
  </div>
  <footer>
    {creationCount} FINALIZED CREATION EVENTS
    {#if resolvedTargetCount} · {resolvedTargetCount} RESOLVED TARGET{/if}
  </footer>
</div>

<style>
  .card { border: 1.5px solid var(--outline-variant); background: var(--surface-container-lowest); }
  header, footer { padding: 7px 10px; background: var(--surface-container-low); color: var(--on-surface-variant); font: 700 10px var(--font-meta); }
  header { display: flex; justify-content: space-between; border-bottom: 1px solid var(--outline-variant); }
  footer { border-top: 1px solid var(--outline-variant); }
  button { padding: 3px 7px; background: var(--surface-container-high); border: 1px solid var(--outline-variant); color: var(--on-surface); font: 700 10px var(--font-meta); }
  button:disabled { opacity: .45; }
  .settings { display: flex; align-items: center; gap: 4px; }
  .settings label { color: var(--on-surface-variant); font: 700 8px var(--font-meta); }
  select { height: 24px; padding: 2px 5px; background: var(--surface-container-high); border: 1px solid var(--outline-variant); color: var(--on-surface); font: 700 9px var(--font-meta); }
  .scroll { overflow-x: auto; max-height: 260px; }
  table { width: 100%; table-layout: fixed; border-collapse: collapse; font: 10px var(--font-data); }
  th:nth-child(1) { width: 24%; } th:nth-child(2) { width: 14%; } th:nth-child(3) { width: 17%; } th:nth-child(4) { width: 14%; } th:nth-child(5) { width: 19%; } th:nth-child(6) { width: 12%; }
  th, td { padding: 7px 10px; text-align: left; border-bottom: 1px solid var(--outline-variant); }
  th { color: var(--on-surface-variant); background: var(--surface-container-low); font: 700 9.5px var(--font-meta); }
  td { color: var(--on-surface); }
  td strong { display: block; overflow: hidden; color: var(--stark-white); text-overflow: ellipsis; white-space: nowrap; }
  small { display: block; overflow: hidden; margin-top: 2px; color: var(--on-surface-variant); font-size: 8px; text-overflow: ellipsis; white-space: nowrap; }
  .empty { color: var(--on-surface-variant); }
  td.created { white-space: nowrap; }
  .links { display: flex; gap: 4px; white-space: nowrap; }
  a { padding: 2px 4px; border: 1px solid var(--outline-variant); color: var(--surface-tint); text-decoration: none; font: 700 8.5px var(--font-meta); }
  .status { padding: 2px 5px; border: 1px solid var(--green-ok); color: var(--green-ok); font: 800 9px var(--font-meta); }
</style>
