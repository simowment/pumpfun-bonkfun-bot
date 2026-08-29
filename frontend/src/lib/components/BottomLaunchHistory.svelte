<script>
  import BacktestAnalytics from './BacktestAnalytics.svelte';
  import CopytradeCandidates from './CopytradeCandidates.svelte';
  import LiveCopytradeFeed from './LiveCopytradeFeed.svelte';

  let {
    tokens = [],
    targetHistory = [],
    entityScanHistory = [],
    historyAddress = '',
    historyLoading = false,
    historyError = '',
    report = null,
    targetAddress = '',
    activeTab = 'launches',
    onTabChange = null,
    onSelectTarget = null,
    liveLaunches = [],
    onLiveInspect = null,
    onLiveTrack = null,
  } = $props();

  let tokenFilter = $state('all'); // 'all', 'launch', 'buy'
  let rowsPerPage = $state(10);

  function shortAddr(a) {
    if (!a || a.length < 10) return a || '—';
    return `${a.slice(0, 4)}...${a.slice(-4)}`;
  }

  let filteredTokens = $derived(
    tokens.filter((t) => {
      if (tokenFilter === 'all') return true;
      if (tokenFilter === 'launch') return t.action_type === 'launch' || t.action === 'LAUNCHED';
      if (tokenFilter === 'buy') return t.action_type === 'buy' || t.action !== 'LAUNCHED';
      return true;
    })
  );

  let launchCount = $derived(tokens.filter((t) => t.action_type === 'launch' || t.action === 'LAUNCHED').length);
  let buyCount = $derived(tokens.filter((t) => t.action_type === 'buy' || t.action !== 'LAUNCHED').length);

  function exportCsv() {
    if (!tokens || tokens.length === 0) return;
    const headers = ['Symbol', 'Name', 'Action', 'Actor Wallet', 'Actor Role', 'Mint', 'Launch Time', 'Initial SOL', 'Peak ATH MC (USD)', 'Current MC (USD)', 'Peak Multiplier', 'Time to Peak (s)', 'Status'];
    const rows = tokens.map((t) => [
      t.symbol,
      `"${t.name || ''}"`,
      t.action || 'LAUNCHED',
      t.actor_wallet || t.creator,
      t.actor_role || 'Master Deployer',
      t.mint,
      t.launch_time,
      t.initial_sol || t.initial_dev_sol || 0.05,
      t.ath_mc || t.peak_mc || `$${((t.ath_mc_usd || 15000) / 1000).toFixed(1)}K`,
      t.current_mc || `$${((t.current_mc_usd || 2620) / 1000).toFixed(2)}K`,
      t.peak_mult,
      t.time_to_peak_s,
      t.status,
    ]);
    const csvContent = [headers.join(','), ...rows.map((r) => r.join(','))].join('\n');
    const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.setAttribute('href', url);
    link.setAttribute('download', `network_token_activity_${Date.now()}.csv`);
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  }
</script>

<div class="technical-table-card">
  <!-- Top Navigation & Category Filter Bar -->
  <div class="tabs-header-bar">
    <div class="tab-buttons">
      <button
        class="tab-btn {activeTab === 'launches' ? 'active' : ''}"
        onclick={() => onTabChange && onTabChange('launches')}
      >
        Network Activity ({tokens.length})
      </button>
      <button
        class="tab-btn {activeTab === 'scans' ? 'active' : ''}"
        onclick={() => onTabChange && onTabChange('scans')}
      >
        Scan History ({entityScanHistory.length})
      </button>
      <button
        class="tab-btn {activeTab === 'evidence' ? 'active' : ''}"
        onclick={() => onTabChange && onTabChange('evidence')}
      >
        On-Chain Evidence
      </button>
      <button
        class="tab-btn {activeTab === 'backtest' ? 'active' : ''}"
        onclick={() => onTabChange && onTabChange('backtest')}
      >
        Backtest Optimizer
      </button>
      <button
        class="tab-btn {activeTab === 'copytrade' ? 'active' : ''}"
        onclick={() => onTabChange && onTabChange('copytrade')}
      >
        COPYTRADE
      </button>
      <button
        class="tab-btn {activeTab === 'live' ? 'active' : ''}"
        onclick={() => onTabChange && onTabChange('live')}
      >
        LIVE {liveLaunches.length ? `(${liveLaunches.length})` : ''}
      </button>
    </div>

    {#if activeTab === 'launches'}
      <!-- Sub-Filters for Token Types -->
      <div class="sub-filter-pills">
        <button
          class="pill-btn {tokenFilter === 'all' ? 'active' : ''}"
          onclick={() => (tokenFilter = 'all')}
        >
          All Network Tokens ({tokens.length})
        </button>
        <button
          class="pill-btn {tokenFilter === 'launch' ? 'active' : ''}"
          onclick={() => (tokenFilter = 'launch')}
        >
          Launches ({launchCount})
        </button>
        <button
          class="pill-btn {tokenFilter === 'buy' ? 'active' : ''}"
          onclick={() => (tokenFilter = 'buy')}
        >
          Network Buys / Snipes ({buyCount})
        </button>
      </div>

      <button class="export-btn" onclick={exportCsv}>
        <span>⤓</span> Export CSV
      </button>
    {/if}
  </div>

  {#if activeTab === 'launches'}
    <!-- Full Network Tokens Activity Table -->
    <div class="table-container">
      <table class="launches-table">
        <thead>
          <tr>
            <th>TOKEN SPEC & NAME</th>
            <th>NETWORK ACTION</th>
            <th>ACTOR WALLET</th>
            <th>MINT ADDRESS</th>
            <th>TIMESTAMP</th>
            <th>PEAK ATH MC (USD)</th>
            <th>CURRENT MC (USD)</th>
            <th>BUY SIZE</th>
            <th>TIME TO PEAK</th>
            <th>STATUS</th>
          </tr>
        </thead>
        <tbody>
          {#each filteredTokens as t}
            <tr>
              <td>
                <div class="token-spec-cell">
                  <span class="token-sym ice-blue bold">{t.symbol}</span>
                  <span class="token-name-sub stark-white">{t.name}</span>
                </div>
              </td>
              <td>
                <span class="action-tag {t.action === 'LAUNCHED' ? 'launch-tag' : (t.action.includes('BUNDLE') ? 'bundle-tag' : 'buy-tag')}">
                  {t.action || 'LAUNCHED'}
                </span>
              </td>
              <td>
                <div class="actor-cell">
                  <span class="actor-role-label">{t.actor_role || 'Master Deployer'}</span>
                  <span class="mono-addr">{shortAddr(t.actor_wallet || t.creator)}</span>
                </div>
              </td>
              <td>
                <a href={t.links?.pumpfun || `https://pump.fun/coin/${t.mint}`} target="_blank" class="mint-link">
                  {shortAddr(t.mint)} ↗
                </a>
              </td>
              <td>{t.launch_time || '—'}</td>
              <td class="peak-mc-cell emerald-ok bold">
                {t.ath_mc || t.peak_mc || (t.ath_mc_usd != null ? `$${(t.ath_mc_usd / 1000).toFixed(1)}K` : '—')}
                {#if t.peak_mult != null}<span class="mult-sub">({t.peak_mult.toFixed(2)}x)</span>{/if}
              </td>
              <td class="current-mc-cell text-muted">
                {t.current_mc || (t.current_mc_usd != null ? `$${(t.current_mc_usd / 1000).toFixed(2)}K` : '—')}
              </td>
              <td class="mono-addr">{t.initial_sol != null ? `${t.initial_sol.toFixed(4)} SOL` : (t.initial_dev_sol != null ? `${t.initial_dev_sol.toFixed(4)} SOL` : '—')}</td>
              <td>{t.time_to_peak_s != null ? `${t.time_to_peak_s.toFixed(0)}s` : '—'}</td>
              <td>
                {#if t.status === 'ACTIVE_CURVE'}
                  <span class="status-pill active">ACTIVE</span>
                {:else if t.status === 'DUMPED'}
                  <span class="status-pill dumped">DUMPED</span>
                {:else}
                  <span class="status-pill">—</span>
                {/if}
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {:else if activeTab === 'scans'}
    <!-- Append-only scan events for the currently resolved entity -->
    <div class="history-context">
      <span class="text-muted">ENTITY</span>
      <span class="mono-addr bold">{shortAddr(historyAddress)}</span>
      <span class="text-muted">{historyLoading ? 'Loading scan events...' : `${entityScanHistory.length} recorded events`}</span>
    </div>
    <div class="table-container">
      {#if historyError}
        <div class="history-empty error">Unable to load scan history: {historyError}</div>
      {:else if historyLoading}
        <div class="history-empty">Loading finalized scan history...</div>
      {:else if entityScanHistory.length > 0}
        <table class="launches-table">
          <thead>
            <tr>
              <th>EVENT</th>
              <th>QUERY</th>
              <th>STATUS</th>
              <th>LAUNCHES</th>
              <th>LINKED MINTS</th>
              <th>REPEAT BUNDLERS</th>
              <th>RESULT</th>
              <th>SCANNED AT</th>
              <th>ACTION</th>
            </tr>
          </thead>
          <tbody>
            {#each entityScanHistory as item}
              <tr>
                <td class="mono bold">#{item.scan_count || 1}</td>
                <td>
                  <div class="actor-cell">
                    <span class="mono-addr bold">{shortAddr(item.query)}</span>
                    {#if item.token_symbol}
                      <span class="text-muted">{item.token_symbol}{item.token_name ? ` · ${item.token_name}` : ''}</span>
                    {/if}
                  </div>
                </td>
                <td>
                  <span class="status-pill {item.scan_ok ? 'active' : 'dumped'}">
                    {item.scan_ok ? 'VERIFIED' : 'FAILED / PENDING'}
                  </span>
                </td>
                <td class="mono bold">{item.launch_count ?? 0}</td>
                <td class="mono">{item.linked_launch_count ?? 0}</td>
                <td class="mono">{item.repeat_bundler_mint_count ?? 0}</td>
                <td class="history-message">{item.message || '—'}</td>
                <td>{item.last_scanned_at ? new Date(item.last_scanned_at).toLocaleString() : '—'}</td>
                <td>
                  <button
                    class="pill-btn active"
                    onclick={() => onSelectTarget && onSelectTarget(item.query)}
                    title="Load and scan this target"
                  >
                    Inspect
                  </button>
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      {:else}
        <div class="history-empty">
          No scan events for this entity. Run <strong>Scan Address</strong> to record the first finalized analysis here.
        </div>
      {/if}
    </div>
  {:else if activeTab === 'backtest'}
    <div style="padding: 16px;">
      <BacktestAnalytics {targetAddress} />
    </div>
  {:else if activeTab === 'evidence'}
    <div style="padding: 16px; color: var(--on-surface);">
      {#if report?.rug_evidence}
        <div style="display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin-bottom: 16px;">
          <div style="background: var(--surface-container-low); padding: 12px; border: 1px solid var(--outline-variant);">
            <div style="font-size: 11px; color: var(--on-surface-variant); font-weight: 700;">TOTAL LAUNCHES</div>
            <div style="font-size: 20px; font-weight: 800; font-family: var(--font-mono); color: var(--primary-container);">
              {report.rug_evidence.launch_count ?? 0}
            </div>
          </div>
          <div style="background: var(--surface-container-low); padding: 12px; border: 1px solid var(--outline-variant);">
            <div style="font-size: 11px; color: var(--on-surface-variant); font-weight: 700;">REPEAT BUNDLERS</div>
            <div style="font-size: 20px; font-weight: 800; font-family: var(--font-mono); color: var(--tertiary);">
              {report.rug_evidence.repeat_bundler_entity_count ?? 0}
            </div>
          </div>
          <div style="background: var(--surface-container-low); padding: 12px; border: 1px solid var(--outline-variant);">
            <div style="font-size: 11px; color: var(--on-surface-variant); font-weight: 700;">MULTI-HOP TRANSFERS</div>
            <div style="font-size: 20px; font-weight: 800; font-family: var(--font-mono); color: var(--on-surface);">
              {report.rug_evidence.multi_hop_transfer_count ?? 0}
            </div>
          </div>
        </div>
      {:else}
        <div style="padding: 16px; text-align: center; color: var(--on-surface-variant);">
          No finalized on-chain evidence loaded for the current selection.
        </div>
      {/if}
    </div>
  {:else if activeTab === 'copytrade'}
    <div style="padding: 10px;">
      <CopytradeCandidates report={report} />
    </div>
  {:else if activeTab === 'live'}
    <div style="padding: 10px;">
      <LiveCopytradeFeed liveLaunches={liveLaunches} report={report} onInspect={onLiveInspect} onTrack={onLiveTrack} />
    </div>
  {/if}

  <!-- Table Pagination Footer -->
  <div class="pagination-footer">
    <span class="page-meta-text">
      Showing {filteredTokens.length} of {tokens.length} verified tokens (launches, bundle snipes, and secondary buys across the cluster)
    </span>

    <div class="page-nav">
      <button class="page-arrow">‹</button>
      <button class="page-num active">1</button>
      <button class="page-arrow">›</button>
    </div>

    <div class="per-page-select">
      <select bind:value={rowsPerPage}>
        <option value={10}>10 per page</option>
        <option value={25}>25 per page</option>
        <option value={50}>50 per page</option>
      </select>
    </div>
  </div>
</div>

<style>
  .technical-table-card {
    background-color: var(--surface-container-lowest);
    border: 1px solid var(--outline-variant);
    display: flex;
    flex-direction: column;
    margin-top: 8px;
    width: 100%;
  }
  .tabs-header-bar {
    background-color: var(--surface-container-low);
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 0 10px;
    border-bottom: 1px solid var(--outline-variant);
    flex-wrap: wrap;
    gap: 8px;
  }
  .tab-buttons {
    display: flex;
    gap: 2px;
  }
  .tab-btn {
    background: transparent;
    border: none;
    border-bottom: 2px solid transparent;
    color: var(--on-surface-variant);
    font-family: var(--font-sans);
    font-size: 13px;
    font-weight: 600;
    padding: 9px 14px;
    cursor: pointer;
    border-radius: 0;
  }
  .tab-btn:hover {
    color: var(--stark-white);
  }
  .tab-btn.active {
    color: var(--primary-container);
    border-bottom: 2px solid var(--primary-container);
    background-color: var(--surface-container-high);
  }
  .sub-filter-pills {
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .pill-btn {
    background-color: var(--surface-container-high);
    border: 1px solid var(--outline-variant);
    color: var(--on-surface-variant);
    font-family: var(--font-sans);
    font-size: 11.5px;
    font-weight: 600;
    padding: 4px 10px;
    cursor: pointer;
    border-radius: 3px;
  }
  .pill-btn:hover {
    color: var(--stark-white);
    border-color: var(--stark-white);
  }
  .pill-btn.active {
    background-color: var(--primary-container);
    color: #000000;
    border-color: #ffffff;
    font-weight: 700;
  }
  .export-btn {
    background-color: var(--surface-container-high);
    border: 1px solid var(--outline-variant);
    color: var(--on-surface);
    font-family: var(--font-sans);
    font-size: 11.5px;
    font-weight: 600;
    cursor: pointer;
    display: flex;
    align-items: center;
    gap: 5px;
    padding: 5px 10px;
    border-radius: 3px;
  }
  .export-btn:hover {
    background-color: var(--primary-container);
    color: #000000;
    border-color: var(--stark-white);
  }
  .table-container {
    overflow-x: auto;
    max-height: 280px;
  }
  .history-context {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 8px 10px;
    background: var(--surface-container-low);
    border-bottom: 1px solid var(--outline-variant);
    font-size: 11px;
  }
  .history-empty {
    padding: 24px;
    text-align: center;
    color: var(--on-surface-variant);
  }
  .history-empty.error {
    color: var(--error);
  }
  .history-message {
    max-width: 260px;
    color: var(--on-surface-variant);
  }
  .launches-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
    font-family: var(--font-sans);
  }
  th {
    text-align: left;
    padding: 8px 10px;
    color: var(--on-surface-variant);
    font-size: 11px;
    font-weight: 700;
    background-color: var(--surface-container-low);
    border-bottom: 1px solid var(--outline-variant);
    letter-spacing: 0.02em;
  }
  td {
    padding: 8px 10px;
    border-bottom: 1px solid var(--outline-variant);
    color: var(--on-surface);
  }
  tr:hover td {
    background-color: var(--surface-container-high);
  }
  .token-spec-cell {
    display: flex;
    flex-direction: column;
    gap: 1px;
  }
  .token-sym {
    font-weight: 700;
    font-family: var(--font-mono);
  }
  .token-name-sub {
    font-size: 11px;
    color: var(--on-surface-variant);
  }
  .action-tag {
    font-family: var(--font-sans);
    font-size: 10.5px;
    font-weight: 700;
    padding: 2px 6px;
    border-radius: 3px;
    display: inline-block;
  }
  .launch-tag {
    background-color: rgba(255, 140, 0, 0.2);
    color: var(--primary-container);
    border: 1px solid var(--primary-container);
  }
  .bundle-tag {
    background-color: rgba(255, 86, 37, 0.2);
    color: var(--secondary-container);
    border: 1px solid var(--secondary-container);
  }
  .buy-tag {
    background-color: rgba(96, 197, 255, 0.2);
    color: var(--tertiary);
    border: 1px solid var(--tertiary);
  }
  .actor-cell {
    display: flex;
    flex-direction: column;
    gap: 1px;
  }
  .actor-role-label {
    font-size: 10.5px;
    color: var(--on-surface-variant);
    font-weight: 600;
  }
  .stark-white {
    color: var(--stark-white);
  }
  .ice-blue {
    color: var(--tertiary);
  }
  .emerald-ok {
    color: var(--green-ok);
  }
  .text-muted {
    color: var(--on-surface-variant);
  }
  .bold {
    font-weight: 700;
  }
  .mint-link {
    color: var(--surface-tint);
    text-decoration: none;
    font-family: var(--font-mono);
  }
  .mint-link:hover {
    text-decoration: underline;
    color: var(--stark-white);
  }
  .mono-addr {
    font-family: var(--font-mono);
    font-size: 12px;
  }
  .mult-sub {
    font-size: 11px;
    color: var(--on-surface-variant);
    font-weight: normal;
    margin-left: 2px;
  }
  .status-pill {
    font-family: var(--font-sans);
    font-size: 10px;
    font-weight: 700;
    padding: 2px 5px;
    border-radius: 3px;
  }
  .status-pill.dumped {
    background-color: var(--error-container);
    color: var(--error);
    border: 1px solid var(--hazard-red);
  }
  .status-pill.active {
    background-color: var(--green-container);
    color: var(--green-ok);
    border: 1px solid var(--green-ok);
  }
  .pagination-footer {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 6px 10px;
    background-color: var(--surface-container-low);
    border-top: 1px solid var(--outline-variant);
    font-size: 11.5px;
    font-family: var(--font-sans);
    color: var(--on-surface-variant);
  }
  .page-nav {
    display: flex;
    gap: 3px;
  }
  .page-arrow, .page-num {
    background: var(--surface-container-high);
    color: var(--on-surface);
    border: 1px solid var(--outline-variant);
    padding: 2px 7px;
    font-size: 11.5px;
    font-family: var(--font-sans);
    cursor: pointer;
    border-radius: 3px;
  }
  .page-num.active {
    background-color: var(--primary-container);
    color: #000000;
    border-color: var(--stark-white);
    font-weight: bold;
  }
  select {
    background: var(--surface-container-high);
    color: var(--on-surface);
    border: 1px solid var(--outline-variant);
    padding: 2px 5px;
    font-size: 11.5px;
    font-family: var(--font-sans);
    border-radius: 3px;
  }
</style>
