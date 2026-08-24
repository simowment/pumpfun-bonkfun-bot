<script>
  import { accountLinks } from '../externalLinks.js';

  let { selectedWallet = null, onTrack = null, onExpand = null } = $props();
  const wallet = $derived(selectedWallet || {});
  const links = $derived(wallet.address ? accountLinks(wallet.address) : null);
  let copied = $state(false);
  function short(address) { return address ? `${address.slice(0, 4)}...${address.slice(-4)}` : '—'; }
  function copy() {
    if (!wallet.address) return;
    navigator.clipboard.writeText(wallet.address);
    copied = true;
    setTimeout(() => (copied = false), 2000);
  }
  function amount(row) {
    return typeof row.amount_lamports === 'number'
      ? `${(row.amount_lamports / 1_000_000_000).toFixed(4)} SOL`
      : '—';
  }
  function time(value) {
    if (!value) return '—';
    return new Date(typeof value === 'number' ? value * 1000 : value).toLocaleString();
  }
</script>

<section>
  <header><b>WALLET INSPECTOR</b><span>NODE</span></header>
  <div class="body">
    <div class="identity"><strong>{short(wallet.address)}</strong><button onclick={copy}>{copied ? '✓ COPIED' : '⧉ COPY'}</button></div>
    <div class="tags"><span>{wallet.role || 'NO WALLET SELECTED'}</span><span>{wallet.balance || 'BALANCE UNKNOWN'}</span></div>
    <dl>
      <dt>FIRST DETECTED</dt><dd>{wallet.firstSeen || '—'}</dd><dt>LAST EMISSION</dt><dd>{wallet.lastActive || '—'}</dd>
      <dt>INFLOW VOLUME</dt><dd>{wallet.inboundSol || '—'}</dd><dt>OUTFLOW VOLUME</dt><dd>{wallet.outboundSol || '—'}</dd>
      <dt>MINTS CREATED</dt><dd>{wallet.totalLaunches ?? '—'}</dd><dt>CLUSTER DEGREE</dt><dd>{wallet.linkedWallets ?? '—'}</dd>
    </dl>
    <h3>ON-CHAIN AUDIT TRANSITS</h3>
    <div class="transfers">
      {#each wallet.transfers || [] as row (`${row.signature}:${row.event_index}`)}
        <div><small>{time(row.timestamp)}</small><b>{amount(row)}</b><span>{short(row.source)} → {short(row.target)}</span></div>
      {:else}<p>No finalized native transfer loaded.</p>{/each}
    </div>
    <div class="actions">
      <button class="track" onclick={() => onTrack?.(wallet)} disabled={!wallet.canTrack}>TRACK</button>
      <button onclick={() => onExpand?.(wallet)} disabled={!wallet.address}>🔍 EXPAND</button><button onclick={copy} disabled={!wallet.address}>📋 COPY</button>
    </div>
    {#if links}
      <div class="external-links">
        <a href={links.gmgn} target="_blank" rel="noreferrer">OPEN GMGN ↗</a>
        <a href={links.solscan} target="_blank" rel="noreferrer">SOLSCAN ↗</a>
      </div>
    {/if}
  </div>
</section>

<style>
  section { background: var(--surface-container-lowest); border: 1.5px solid var(--outline-variant); }
  header { display: flex; justify-content: space-between; padding: 6px 10px; background: var(--primary-container); color: #000; font: 800 11px var(--font-headline); }
  .body { display: flex; flex-direction: column; gap: 8px; padding: 9px; }
  .identity { display: flex; justify-content: space-between; color: var(--tertiary); font: 800 13px var(--font-data); }
  button { padding: 3px 6px; background: var(--surface-container-high); border: 1px solid var(--outline-variant); color: var(--on-surface); font: 700 9px var(--font-meta); }
  button:disabled { opacity: .45; }
  .tags { display: flex; gap: 4px; } .tags span { padding: 2px 4px; border: 1px solid var(--primary-container); color: var(--primary-container); font: 800 9px var(--font-meta); }
  dl { display: grid; grid-template-columns: 1fr 1fr; gap: 3px 8px; margin: 0; padding: 6px; background: var(--surface-container-low); border: 1px solid var(--outline-variant); }
  dt { color: var(--on-surface-variant); font: 9px var(--font-meta); } dd { margin: 0; color: var(--on-surface); font: 11px var(--font-data); }
  h3 { margin: 0; color: var(--on-surface-variant); font: 800 9.5px var(--font-meta); }
  .transfers { display: flex; flex-direction: column; gap: 2px; max-height: 300px; overflow: auto; }
  .transfers div { display: grid; grid-template-columns: 1fr auto; gap: 2px 5px; padding: 3px 6px; background: var(--surface-container-low); border: 1px solid var(--outline-variant); color: var(--on-surface); font: 10px var(--font-data); }
  .transfers div span { grid-column: span 2; } .transfers p, small { color: var(--on-surface-variant); }
  .actions { display: flex; gap: 4px; } .actions button { flex: 1; } .track { border-color: var(--primary-container); color: var(--primary-container); }
  .external-links { display: flex; gap: 4px; }
  .external-links a { flex: 1; padding: 4px; border: 1px solid var(--outline-variant); text-align: center; background: var(--surface-container-high); color: var(--on-surface); text-decoration: none; font: 700 9px var(--font-meta); }
</style>
