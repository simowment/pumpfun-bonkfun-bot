<script>
  let { report = null, clusterId = '—' } = $props();

  const stats = $derived(report?.stats ?? {});
  const evidence = $derived(report?.rug_evidence ?? null);
  const bundles = $derived(report?.launch_bundles ?? null);
  const crossBundles = $derived(report?.cross_entity_bundles ?? null);

  const slot = (v) => (v != null ? `Slot ${v}` : '—');
  const sol = (lamports) =>
    lamports != null ? `${(lamports / 1e9).toFixed(4)} SOL` : '—';
  const short = (w) => (w ? `${w.slice(0, 4)}…${w.slice(-4)}` : '—');
</script>

<section class="manifest">
  <header>
    <b>▲ ENTITY MANIFEST</b>
    <span>{report ? 'LIVE STATE' : 'NO SCAN'}</span>
  </header>
  <div class="summary">
    <dl>
      <dt>CLUSTER ID</dt><dd class="ice">{clusterId}</dd>
      <dt>TARGET</dt><dd>{report?.target_wallet || '—'}</dd>
      <dt>AS OF SLOT</dt><dd>{report?.as_of_slot ?? '—'}</dd>
      <dt>FIRST SEEN</dt><dd>{slot(stats.first_seen_slot)}</dd>
      <dt>LAST ACTIVE</dt><dd>{slot(stats.last_seen_slot)}</dd>
      <dt>NODES</dt><dd>{report?.graph?.nodes?.length ?? 0}</dd>
      <dt>SCANNED TX</dt><dd>{stats.scanned_transaction_count ?? 0}</dd>
      <dt>FINALIZED TX</dt><dd>{stats.successful_transaction_count ?? 0}</dd>
    </dl>
  </div>
  <div class="subsection">
    <h3>OPERATOR ATTRIBUTES <span>FINALIZED</span></h3>
    <dl>
      <dt>LAUNCHES</dt><dd>{stats.launch_count ?? 0}</dd>
      <dt>EARLY LAUNCHES</dt><dd>{stats.early_launch_count ?? 0}</dd>
      <dt>LINKED LAUNCHES</dt><dd>{stats.linked_launch_count ?? 0}</dd>
      <dt>LINKED WALLETS</dt><dd>{stats.direct_linked_wallet_count ?? 0}</dd>
      <dt>LINKED CREATORS</dt><dd>{stats.linked_creator_wallet_count ?? 0}</dd>
      <dt>REPEAT BUNDLERS</dt><dd>{stats.repeat_bundler_entity_count ?? 0}</dd>
      <dt>WALLET SWITCHES</dt><dd>{stats.wallet_switch_count ?? 0}</dd>
      <dt>FRESH WALLETS</dt><dd>{stats.fresh_wallet_count ?? 0}</dd>
      <dt>MULTI-HOP XFER</dt><dd>{stats.multi_hop_count ?? 0}</dd>
      <dt>PUMP BUYS</dt><dd>{stats.pump_buy_count ?? 0}</dd>
      <dt>PUMP SELLS</dt><dd>{stats.pump_sell_count ?? 0}</dd>
      <dt>TRADED MINTS</dt><dd>{stats.traded_mint_count ?? 0}</dd>
      <dt>NATIVE IN</dt><dd>{sol(stats.native_in_lamports)}</dd>
      <dt>NATIVE OUT</dt><dd>{sol(stats.native_out_lamports)}</dd>
    </dl>
  </div>
  <div class="subsection">
    <h3>
      LAUNCH BUNDLES
      <span>{bundles ? `${bundles.bundled_launch_count ?? 0} BUNDLED · ${bundles.repeat_bundler_count ?? 0} CREW` : 'NONE'}</span>
    </h3>
    {#if bundles?.launches?.length}
      <table>
        <thead><tr><th>LAUNCH</th><th>SIZE</th><th>FIRST BUYER</th><th>TX IDX</th><th>TOTAL</th></tr></thead>
        <tbody>
          {#each bundles.launches as launch}
            <tr>
              <td>{launch.symbol || launch.mint.slice(0, 6)}</td>
              <td>{launch.bundle_size}</td>
              <td>{short(launch.first_buyer_wallet)}</td>
              <td>{launch.first_buy_transaction_index ?? '—'}</td>
              <td>{sol(launch.total_max_sol_lamports)}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    {:else}
      <ul><li>No creation-slot buy evidence.</li></ul>
    {/if}
  </div>
  <div class="subsection">
    <h3>
      CROSS-ENTITY BUNDLERS
      <span>{crossBundles ? `${crossBundles.wallet_count ?? 0} WALLETS · ${crossBundles.linked_entity_count ?? 0} CREATORS` : 'NONE'}</span>
    </h3>
    {#if crossBundles?.wallets?.length}
      <table>
        <thead><tr><th>BUNDLER</th><th>CREATORS</th><th>BUYS</th><th>EXTERNAL TARGETS</th></tr></thead>
        <tbody>
          {#each crossBundles.wallets as item}
            <tr>
              <td>{short(item.wallet)}</td>
              <td>{item.external_creator_count}</td>
              <td>{item.buys?.length ?? 0}</td>
              <td>{item.external_creators ? item.external_creators.map(short).join(', ') : '—'}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    {:else}
      <ul><li>No cross-entity bundle links detected.</li></ul>
    {/if}
  </div>
  <div class="subsection">
    <h3>ON-CHAIN EVIDENCE
      <span>{evidence ? 'SUMMARY' : 'NONE'}</span>
    </h3>
    {#if evidence}
      <ul>
        <li>Launches observed: {evidence.launch_count ?? 0} (linked: {evidence.linked_launch_count ?? 0})</li>
        <li>Block-0/1 position launches: {evidence.early_position_launch_count ?? 0}</li>
        <li>Direct linked wallets: {evidence.direct_linked_wallet_count ?? 0}</li>
        <li>Linked creator wallets: {evidence.linked_creator_wallet_count ?? 0}</li>
        <li>Wallet switch candidate: {evidence.wallet_switch_candidate ? 'YES' : 'NO'} ({evidence.wallet_switch_count ?? 0} observed)</li>
        <li>Proven fresh wallets: {evidence.fresh_wallet_proven_count ?? 0}</li>
        <li>Multi-hop transfers: {evidence.multi_hop_transfer_count ?? 0}</li>
        <li>Repeat bundler entities: {evidence.repeat_bundler_entity_count ?? 0} ({evidence.repeat_bundler_mint_count ?? 0} mints)</li>
        <li>Native in: {sol(evidence.native_in_lamports)} · out: {sol(evidence.native_out_lamports)}</li>
        <li>Indexed creations: {evidence.indexed_created_count ?? '—'} · history bounded: {evidence.history_is_bounded ? 'YES' : 'NO'}</li>
      </ul>
    {:else}
      <ul><li>No finalized evidence loaded.</li></ul>
    {/if}
  </div>
</section>

<style>
  .manifest { background: var(--surface-container-lowest); border: 1.5px solid var(--outline-variant); }
  header { display: flex; justify-content: space-between; padding: 6px 10px; background: var(--primary-container); color: #000; font: 800 10px var(--font-headline); }
  header span { font: 700 9px var(--font-meta); }
  .summary { padding: 8px 10px; }
  dl { display: grid; grid-template-columns: 96px minmax(0, 1fr); gap: 4px 7px; margin: 0; font: 10px var(--font-data); }
  dt { color: var(--on-surface-variant); font: 9px var(--font-meta); }
  dd { min-width: 0; margin: 0; overflow-wrap: anywhere; color: var(--on-surface); }
  .ice { color: var(--tertiary); }
  .subsection { padding: 7px 10px; border-top: 1px solid var(--outline-variant); }
  h3 { display: flex; justify-content: space-between; margin: 0 0 6px; color: var(--on-surface-variant); font: 800 8px var(--font-meta); letter-spacing: .04em; }
  h3 span { color: var(--tertiary); }
  ul { max-height: 200px; margin: 0; padding: 0; overflow-y: auto; color: var(--on-surface); font: 9px var(--font-data); list-style: none; }
  li { position: relative; margin: 4px 0; padding-left: 10px; overflow-wrap: anywhere; }
  li::before { position: absolute; left: 0; content: '▸'; color: var(--primary-container); }
  table { width: 100%; margin: 0; border-collapse: collapse; font: 9px var(--font-data); color: var(--on-surface); }
  th { padding: 2px 4px; text-align: left; color: var(--on-surface-variant); font: 800 8px var(--font-meta); letter-spacing: .04em; }
  td { padding: 3px 4px; overflow-wrap: anywhere; border-top: 1px solid var(--outline-variant); }
</style>
