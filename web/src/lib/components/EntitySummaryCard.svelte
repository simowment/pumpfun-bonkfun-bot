<script>
  let {
    clusterId = '—', totalWallets = 0, firstSeen = '—', lastSeen = '—',
    evidence = [],
  } = $props();
</script>

<section class="manifest">
  <header><b>▲ ENTITY MANIFEST</b><span>LIVE STATE</span></header>
  <div class="summary">
    <dl>
      <dt>CLUSTER ID</dt><dd class="ice">{clusterId}</dd>
      <dt>NODES</dt><dd>{totalWallets}</dd>
      <dt>FIRST SEEN</dt><dd>{firstSeen}</dd>
      <dt>LAST ACTIVE</dt><dd>{lastSeen}</dd>
    </dl>
  </div>
  <div class="subsection">
    <h3>ON-CHAIN EVIDENCE <span>{evidence.length}</span></h3>
    <ul>
      {#each evidence as item (item)}
        <li>{item}</li>
      {:else}
        <li>No finalized evidence loaded.</li>
      {/each}
    </ul>
  </div>
</section>

<style>
  .manifest { background: var(--surface-container-lowest); border: 1.5px solid var(--outline-variant); }
  header { display: flex; justify-content: space-between; padding: 6px 10px; background: var(--primary-container); color: #000; font: 800 10px var(--font-headline); }
  header span { font: 700 9px var(--font-meta); }
  .summary { padding: 8px 10px; }
  dl { display: grid; grid-template-columns: 82px minmax(0, 1fr); gap: 5px 7px; margin: 0; font: 10px var(--font-data); }
  dt { color: var(--on-surface-variant); font: 9px var(--font-meta); }
  dd { min-width: 0; margin: 0; overflow-wrap: anywhere; color: var(--on-surface); }
  .ice { color: var(--tertiary); }
  .subsection { padding: 7px 10px; border-top: 1px solid var(--outline-variant); }
  h3 { display: flex; justify-content: space-between; margin: 0 0 6px; color: var(--on-surface-variant); font: 800 8px var(--font-meta); letter-spacing: .04em; }
  h3 span { color: var(--tertiary); }
  ul { max-height: 170px; margin: 0; padding: 0; overflow-y: auto; color: var(--on-surface); font: 9px var(--font-data); list-style: none; }
  li { position: relative; margin: 4px 0; padding-left: 10px; overflow-wrap: anywhere; }
  li::before { position: absolute; left: 0; content: '▸'; color: var(--primary-container); }
</style>
