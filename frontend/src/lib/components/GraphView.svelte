<script>
  import { fetchCachedEntity, scanEntity } from '../api.js';
  import { accountLinks } from '../externalLinks.js';
  import { formatSol, shortId } from '../format.js';

  let {
    cluster = null,
    entity = null,
    onSelectWallet = null,
  } = $props();

  let chain = $state([]);
  let lastRoot = '';
  let loadingAddress = $state('');
  let traceError = $state('');
  let expandedCards = $state({});
  let selectedWindow = $state('all');

  const MAX_TRACE_HOPS = 8;
  const MAX_VISIBLE_ROWS = 5;
  const WINDOWS = [
    { value: '24h', label: '24h', seconds: 86400 },
    { value: '7d', label: '7 days', seconds: 604800 },
    { value: '30d', label: '30 days', seconds: 2592000 },
    { value: 'all', label: 'All', seconds: null },
  ];

  const report = $derived(entity ?? cluster);
  const rootAddress = $derived(
    report?.tracking_address || report?.target_wallet || report?.identity?.input || '',
  );
  const traceRoot = $derived(chain[0] || { wallet: rootAddress, report, mode: 'root', via: null });
  const incomingRows = $derived(traceRoot ? rowsFor({ ...traceRoot, mode: 'to' }, 'in') : []);
  const outgoingRows = $derived(traceRoot ? rowsFor({ ...traceRoot, mode: 'from' }, 'out') : []);

  $effect(() => {
    if (rootAddress && rootAddress !== lastRoot) {
      lastRoot = rootAddress;
      chain = [{ wallet: rootAddress, report, mode: 'root', via: null }];
      expandedCards = {};
      traceError = '';
    }
  });

  function symbolFor(reportData, mint) {
    const item = (reportData?.entity_mints || []).find((entry) => entry?.mint === mint);
    return item?.symbol || item?.name || shortId(mint, 4, 4);
  }

  function latestTimestamp(reportData, source, target) {
    const timestamps = (reportData?.transfers || [])
      .filter((transfer) => transfer?.source === source && transfer?.target === target)
      .map((transfer) => Number(transfer.timestamp))
      .filter((timestamp) => Number.isFinite(timestamp) && timestamp > 0);
    return timestamps.length ? Math.max(...timestamps) : null;
  }

  function assetKind(edge) {
    return edge?.asset_kind || (edge?.kind === 'direct_spl_transfer' ? 'token' : 'native');
  }

  function edgeRows(reportData, wallet, direction) {
    const edges = reportData?.graph?.edges || reportData?.graph?.links || [];
    const grouped = new Map();

    for (const edge of edges) {
      const source = edge?.source || '';
      const target = edge?.target || '';
      if (!source || !target || source === target) continue;
      const matches = direction === 'out' ? source === wallet : target === wallet;
      if (!matches) continue;

      const peer = direction === 'out' ? target : source;
      const kind = assetKind(edge);
      const assetId = edge?.asset_id || (kind === 'native' ? 'SOL' : 'TOKEN');
      const key = `${peer}:${kind}:${assetId}`;
      const current = grouped.get(key) || {
        key,
        peer,
        kind,
        assetId,
        symbol: kind === 'native' ? 'SOL' : symbolFor(reportData, assetId),
        volumeLamports: 0,
        volumeBaseUnits: 0,
        transfers: 0,
        lastSlot: 0,
        lastTimestamp: null,
        source,
        target,
      };

      current.volumeLamports += Number(edge?.amount_lamports || 0);
      current.volumeBaseUnits += Number(edge?.amount_base_units || 0);
      current.transfers += Number(edge?.transfer_count || 0);
      current.lastSlot = Math.max(current.lastSlot, Number(edge?.last_slot || 0));
      const timestamp = latestTimestamp(reportData, source, target);
      current.lastTimestamp = Math.max(current.lastTimestamp || 0, timestamp || 0) || null;
      grouped.set(key, current);
    }

    return [...grouped.values()].sort((left, right) => {
      const leftVolume = left.kind === 'native' ? left.volumeLamports : left.volumeBaseUnits;
      const rightVolume = right.kind === 'native' ? right.volumeLamports : right.volumeBaseUnits;
      return rightVolume - leftVolume || right.transfers - left.transfers;
    });
  }

  function inWindow(row) {
    const window = WINDOWS.find((item) => item.value === selectedWindow);
    if (!window || window.seconds === null || !row.lastTimestamp) return true;
    return Math.floor(Date.now() / 1000) - row.lastTimestamp <= window.seconds;
  }

  function rowsFor(card, direction = 'out') {
    return edgeRows(card.report, card.wallet, direction).filter(inWindow);
  }

  function formatTokenUnits(row) {
    if (!row.volumeBaseUnits) return '0';
    return `${new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 2 }).format(row.volumeBaseUnits)} raw`;
  }

  function formatVolume(row) {
    return row.kind === 'native' ? formatSol(row.volumeLamports) : formatTokenUnits(row);
  }

  function formatLast(row) {
    if (row.lastTimestamp) {
      const elapsed = Math.max(0, Math.floor(Date.now() / 1000) - row.lastTimestamp);
      if (elapsed < 60) return `${elapsed}s`;
      if (elapsed < 3600) return `${Math.floor(elapsed / 60)}m`;
      if (elapsed < 86400) return `${Math.floor(elapsed / 3600)}h`;
      return `${Math.floor(elapsed / 86400)}d`;
    }
    return row.lastSlot ? `slot ${row.lastSlot}` : '—';
  }

  function cardRows(card) {
    return rowsFor(card, card.mode === 'to' ? 'in' : 'out');
  }

  function displayRows(card) {
    const rows = cardRows(card);
    return expandedCards[card.id] ? rows : rows.slice(0, MAX_VISIBLE_ROWS);
  }

  function toggleCard(cardId) {
    expandedCards = { ...expandedCards, [cardId]: !expandedCards[cardId] };
  }

  function selectWallet(row) {
    onSelectWallet?.({
      address: row.peer,
      role: 'Cluster Node',
      balance: '—',
      confidence: null,
    });
  }

  async function resolveReport(address, parentReport = null) {
    const parentNodes = parentReport?.graph?.nodes || [];
    const parentEdges = parentReport?.graph?.edges || parentReport?.graph?.links || [];
    const presentInParent = parentNodes.some((node) => (node?.address || node?.id) === address)
      || parentEdges.some((edge) => edge?.source === address || edge?.target === address);
    if (presentInParent) return parentReport;

    try {
      const cached = await fetchCachedEntity(address);
      if (cached?.data) return cached.data;
    } catch {
      // A cache miss is expected before a wallet has been scanned.
    }

    const scanned = await scanEntity(address, 100);
    if (scanned?.ok && scanned.data) return scanned.data;
    throw new Error(scanned?.message || `No finalized report for ${shortId(address)}`);
  }

  async function traceRow(card, row) {
    if (!row?.peer || loadingAddress) return;
    const existing = chain.find((item) => item.wallet === row.peer && item.mode === 'from');
    if (existing) {
      document.getElementById(existing.id)?.scrollIntoView({ behavior: 'smooth', inline: 'center' });
      return;
    }
    if (chain.length >= MAX_TRACE_HOPS + 1) {
      traceError = `Trace limit reached at ${MAX_TRACE_HOPS} hops.`;
      return;
    }

    traceError = '';
    loadingAddress = row.peer;
    try {
      const nextReport = await resolveReport(row.peer, card.report);
      const id = `trace-${chain.length}-${row.peer}`;
      chain = [
        ...chain,
        {
          id,
          wallet: row.peer,
          report: nextReport,
          mode: 'from',
          via: { source: row.source, target: row.target, amount: formatVolume(row) },
        },
      ];
      queueMicrotask(() => document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', inline: 'center' }));
    } catch (error) {
      traceError = error?.message || 'Wallet trace failed.';
    } finally {
      loadingAddress = '';
    }
  }

  function traceTopRow(card) {
    return cardRows(card)[0] || null;
  }

  function traceConnector(card) {
    const row = traceTopRow(card);
    if (row) traceRow(card, row);
  }

  function copyAddress(address) {
    if (address && navigator?.clipboard) navigator.clipboard.writeText(address);
  }
</script>

<section class="trace-panel">
  <header class="trace-header">
    <div class="trace-heading">
      <span class="trace-title">Connected wallets</span>
      {#if rootAddress}
        <button class="address-pill" onclick={() => copyAddress(rootAddress)} title="Copy wallet address">
          <span>{shortId(rootAddress, 6, 6)}</span>
          <span class="copy-icon">⧉</span>
        </button>
      {/if}
      <span class="trace-subtitle">Finalized transfer evidence</span>
    </div>
    <div class="trace-controls">
      <label for="trace-window">Window</label>
      <select id="trace-window" bind:value={selectedWindow}>
        {#each WINDOWS as option}
          <option value={option.value}>{option.label}</option>
        {/each}
      </select>
      {#if chain.length > 1}
        <button class="reset-button" onclick={() => (chain = [chain[0]])}>Reset</button>
      {/if}
    </div>
  </header>

  {#if traceError}
    <div class="trace-error" role="alert">{traceError}</div>
  {/if}

  {#if !rootAddress}
    <div class="trace-empty">Scan a wallet or token to build the transfer trace.</div>
  {:else}
    <div class="trace-scroll">
      <div class="trace-lane">
        <article class="transfer-card" id="root-in">
          <header class="card-header">
            <div class="card-title"><span class="direction to">To</span> {shortId(traceRoot.wallet, 6, 6)} <span class="count-badge">{incomingRows.length}</span></div>
            <span class="window-chip">{WINDOWS.find((item) => item.value === selectedWindow)?.label}</span>
          </header>
          <div class="table-head"><span>Wallet</span><span>Volume ↓</span><span>Transfers</span><span>Last</span><span aria-hidden="true"></span></div>
          {#if incomingRows.length === 0}
            <div class="card-empty">No incoming transfers observed.</div>
          {:else}
            {#each (expandedCards['root-in'] ? incomingRows : incomingRows.slice(0, MAX_VISIBLE_ROWS)) as row (row.key)}
              <div class="transfer-row">
                <button class="wallet-label" onclick={() => selectWallet(row)} title={row.peer}>
                  <span class="asset-dot wallet-dot">W</span>
                  <span class="wallet-text">{shortId(row.peer, 5, 5)}</span>
                  <span class="external">↗</span>
                </button>
                <span class="volume">{formatVolume(row)}{row.kind === 'token' ? ` · ${row.symbol}` : ' SOL'}</span>
                <span class="metric">{row.transfers || 1}</span>
                <span class="last">{formatLast(row)}</span>
                <button class="trace-arrow" class:busy={loadingAddress === row.peer} onclick={() => traceRow({ ...traceRoot, mode: 'to', id: 'root-in' }, row)} title="Continue tracing this wallet" aria-label={`Trace ${shortId(row.peer)}`}>{loadingAddress === row.peer ? '…' : '→'}</button>
              </div>
            {/each}
            {#if incomingRows.length > MAX_VISIBLE_ROWS}
              <button class="show-all" onclick={() => toggleCard('root-in')}>
                {expandedCards['root-in'] ? 'Show less' : `Show all (${incomingRows.length})`}
              </button>
            {/if}
          {/if}
        </article>

        <div class="connector root-connector">
          <span class="connector-line"></span>
          <div class="pivot-card">
            <button class="pivot-address" onclick={() => copyAddress(traceRoot.wallet)} title="Copy pivot wallet">{shortId(traceRoot.wallet, 6, 6)} <span class="copy-icon">⧉</span></button>
            <button class="pivot-expand" onclick={() => traceConnector(traceRoot)} title="Continue tracing">＋</button>
          </div>
          <span class="connector-line"></span>
        </div>

        <article class="transfer-card" id="root-out">
          <header class="card-header">
            <div class="card-title"><span class="direction from">From</span> {shortId(traceRoot.wallet, 6, 6)} <span class="count-badge">{outgoingRows.length}</span></div>
            <span class="window-chip">{WINDOWS.find((item) => item.value === selectedWindow)?.label}</span>
          </header>
          <div class="table-head"><span>Wallet / token</span><span>Volume ↓</span><span>Transfers</span><span>Last</span><span aria-hidden="true"></span></div>
          {#if outgoingRows.length === 0}
            <div class="card-empty">No outgoing transfers observed.</div>
          {:else}
            {#each (expandedCards['root-out'] ? outgoingRows : outgoingRows.slice(0, MAX_VISIBLE_ROWS)) as row (row.key)}
              <div class="transfer-row">
                <button class="wallet-label" onclick={() => selectWallet(row)} title={row.peer}>
                  <span class="asset-dot {row.kind === 'token' ? 'token-dot' : 'wallet-dot'}">{row.kind === 'token' ? '◆' : 'W'}</span>
                  <span class="wallet-text">{shortId(row.peer, 5, 5)}</span>
                  <span class="external">↗</span>
                </button>
                <span class="volume">{formatVolume(row)}{row.kind === 'token' ? ` · ${row.symbol}` : ' SOL'}</span>
                <span class="metric">{row.transfers || 1}</span>
                <span class="last">{formatLast(row)}</span>
                <button class="trace-arrow" class:busy={loadingAddress === row.peer} onclick={() => traceRow({ ...traceRoot, mode: 'from', id: 'root-out' }, row)} title="Continue tracing this wallet" aria-label={`Trace ${shortId(row.peer)}`}>{loadingAddress === row.peer ? '…' : '→'}</button>
              </div>
            {/each}
            {#if outgoingRows.length > MAX_VISIBLE_ROWS}
              <button class="show-all" onclick={() => toggleCard('root-out')}>
                {expandedCards['root-out'] ? 'Show less' : `Show all (${outgoingRows.length})`}
              </button>
            {/if}
          {/if}
        </article>

        {#each chain.slice(1) as card (card.id)}
          <div class="connector" aria-hidden="true">
            <span class="connector-line"></span>
            <button class="connector-button" onclick={() => traceConnector(card)} title="Continue tracing the largest transfer">＋</button>
            <span class="connector-line"></span>
          </div>
          <article class="transfer-card" id={card.id}>
            <header class="card-header">
              <div class="card-title"><span class="direction from">From</span> {shortId(card.wallet, 6, 6)} <span class="count-badge">{cardRows(card).length}</span></div>
              <div class="card-actions">
                <button class="icon-button" onclick={() => copyAddress(card.wallet)} title="Copy wallet address">⧉</button>
                <a class="icon-button" href={accountLinks(card.wallet).solscan} target="_blank" rel="noreferrer" title="Open in Solscan">↗</a>
              </div>
            </header>
            <div class="table-head"><span>Wallet / token</span><span>Volume ↓</span><span>Transfers</span><span>Last</span><span aria-hidden="true"></span></div>
            {#if loadingAddress === card.wallet}
              <div class="card-empty loading">Tracing finalized transfers…</div>
            {:else if cardRows(card).length === 0}
              <div class="card-empty">No outgoing transfers observed for this wallet.</div>
            {:else}
              {#each displayRows(card) as row (row.key)}
                <div class="transfer-row">
                  <button class="wallet-label" onclick={() => selectWallet(row)} title={row.peer}>
                    <span class="asset-dot {row.kind === 'token' ? 'token-dot' : 'wallet-dot'}">{row.kind === 'token' ? '◆' : 'W'}</span>
                    <span class="wallet-text">{shortId(row.peer, 5, 5)}</span>
                    <span class="external">↗</span>
                  </button>
                  <span class="volume">{formatVolume(row)}{row.kind === 'token' ? ` · ${row.symbol}` : ' SOL'}</span>
                  <span class="metric">{row.transfers || 1}</span>
                  <span class="last">{formatLast(row)}</span>
                  <button class="trace-arrow" class:busy={loadingAddress === row.peer} onclick={() => traceRow(card, row)} title="Continue tracing this wallet" aria-label={`Trace ${shortId(row.peer)}`}>{loadingAddress === row.peer ? '…' : '→'}</button>
                </div>
              {/each}
              {#if cardRows(card).length > MAX_VISIBLE_ROWS}
                <button class="show-all" onclick={() => toggleCard(card.id)}>
                  {expandedCards[card.id] ? 'Show less' : `Show all (${cardRows(card).length})`}
                </button>
              {/if}
            {/if}
          </article>
        {/each}
      </div>
    </div>
  {/if}
</section>

<style>
  .trace-panel {
    width: 100%;
    min-width: 0;
    background: #0d101c;
    border: 1px solid #22283b;
    color: #e8ebf5;
    overflow: hidden;
  }

  .trace-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    padding: 12px 14px;
    border-bottom: 1px solid #22283b;
    background: #101525;
  }

  .trace-heading, .trace-controls, .card-title, .card-actions, .address-pill, .pivot-card {
    display: flex;
    align-items: center;
  }

  .trace-heading { gap: 10px; min-width: 0; }
  .trace-title { font-size: 15px; font-weight: 750; white-space: nowrap; }
  .trace-subtitle { color: #77819a; font-size: 11px; white-space: nowrap; }

  .address-pill, .pivot-card, .window-chip {
    border: 1px solid #303a58;
    background: #171d31;
    color: #aeb9e4;
    font: 600 11px var(--font-mono);
    padding: 5px 8px;
    border-radius: 5px;
  }

  .address-pill { gap: 7px; cursor: pointer; }
  .pivot-address { border: 0; padding: 0; color: inherit; background: transparent; font: inherit; cursor: pointer; }
  .address-pill:hover, .pivot-card:hover { border-color: #7c72c8; color: #d8d5ff; }
  .copy-icon { color: #7785ae; }

  .trace-controls { gap: 7px; color: #7f89a5; font-size: 11px; }
  .trace-controls select {
    border: 1px solid #303a58;
    background: #171d31;
    color: #c9d0e8;
    padding: 5px 8px;
    font-size: 11px;
  }
  .reset-button, .show-all, .pivot-expand, .connector-button, .icon-button, .trace-arrow, .wallet-label {
    border: 0;
    cursor: pointer;
  }
  .reset-button { padding: 5px 8px; color: #aeb9e4; background: #25213d; font-size: 11px; }
  .trace-error { padding: 8px 14px; color: #ff9e9e; background: #321c28; font-size: 12px; border-bottom: 1px solid #593244; }
  .trace-empty { padding: 60px 20px; text-align: center; color: #7e89a5; font-size: 13px; }

  .trace-scroll { overflow-x: auto; padding: 38px 14px 46px; }
  .trace-lane { display: flex; align-items: center; min-width: max-content; min-height: 360px; }

  .transfer-card {
    width: 330px;
    background: #151b2c;
    border: 1px solid #293451;
    border-radius: 7px;
    box-shadow: 0 10px 25px rgba(0, 0, 0, .18);
    overflow: hidden;
    flex: 0 0 330px;
  }
  .card-header { min-height: 42px; padding: 8px 10px; justify-content: space-between; gap: 8px; border-bottom: 1px solid #293451; }
  .card-title { gap: 5px; font: 650 12px var(--font-mono); white-space: nowrap; }
  .direction { font: 800 10px var(--font-sans); letter-spacing: .04em; }
  .direction.to { color: #77c9d0; }
  .direction.from { color: #c4a6ef; }
  .count-badge { min-width: 18px; padding: 1px 5px; border-radius: 10px; background: #292348; color: #c9bbff; text-align: center; font: 700 10px var(--font-sans); }
  .window-chip { padding: 3px 6px; font-size: 10px; }
  .card-actions { gap: 3px; }
  .icon-button { display: inline-flex; align-items: center; justify-content: center; padding: 3px 5px; color: #8d99bd; background: transparent; text-decoration: none; }
  .icon-button:hover { color: #eeeaff; background: #252a42; }

  .table-head, .transfer-row { display: grid; grid-template-columns: minmax(94px, 1fr) 60px 43px 40px 24px; align-items: center; column-gap: 3px; }
  .transfer-row { position: relative; }
  .table-head { padding: 7px 8px; color: #687492; font-size: 9px; text-transform: uppercase; letter-spacing: .04em; }
  .transfer-row { min-height: 43px; padding: 5px 8px; border-top: 1px solid #202a42; font-size: 10px; }
  .transfer-row:hover { background: #1b2440; }
  .transfer-row > * { min-width: 0; }
  .trace-arrow { justify-self: end; grid-column: 5; width: 24px; height: 24px; padding: 0; border: 1px solid #6758a2; border-radius: 4px; color: #f0edff; background: #30265b; font: 700 16px/1 var(--font-sans); }
  .trace-arrow:hover, .trace-arrow:focus-visible, .pivot-expand:hover, .connector-button:hover { color: #fff; background: #5c4ba2; }
  .trace-arrow.busy { color: #fff; background: #6e5ab7; cursor: wait; }
  .wallet-label { min-width: 0; display: flex; align-items: center; gap: 5px; padding: 0; color: #cad2e7; background: transparent; text-align: left; font: 600 10px var(--font-mono); }
  .wallet-label:hover { color: #fff; }
  .asset-dot { width: 17px; height: 17px; flex: 0 0 17px; display: inline-flex; align-items: center; justify-content: center; border-radius: 50%; font: 800 8px var(--font-sans); }
  .wallet-dot { color: #9ee6e5; background: #164348; }
  .token-dot { color: #e1c8ff; background: #443069; }
  .wallet-text { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .external { color: #68779f; font: 10px var(--font-sans); }
  .volume, .metric, .last { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .volume { color: #edf0fa; text-align: right; font-family: var(--font-mono); }
  .metric { color: #a5b1d0; text-align: right; }
  .last { color: #8792af; text-align: right; }
  .card-empty { padding: 28px 12px; color: #74809d; text-align: center; font-size: 11px; }
  .loading { color: #c5b7ff; }
  .show-all { width: calc(100% - 12px); margin: 6px; padding: 7px; border: 1px solid #403568; color: #cdbfff; background: #221c3e; font-size: 10px; }
  .show-all:hover { background: #30265a; }

  .connector { width: 110px; flex: 0 0 110px; display: flex; align-items: center; justify-content: center; gap: 0; }
  .connector-line { width: 32px; border-top: 2px dotted #62558d; }
  .connector::before, .connector::after { content: ''; width: 7px; height: 7px; border: 2px solid #7667a8; background: #0d101c; border-radius: 50%; flex: 0 0 auto; }
  .root-connector { width: 170px; flex-basis: 170px; }
  .root-connector .connector-line { width: 23px; }
  .pivot-card { gap: 6px; padding: 8px 7px 8px 10px; cursor: pointer; white-space: nowrap; }
  .pivot-expand, .connector-button { width: 22px; height: 22px; border-radius: 50%; padding: 0; color: #d2caff; background: #35295d; font-size: 15px; line-height: 1; }
  .connector-button { flex: 0 0 22px; }

  @media (max-width: 800px) {
    .trace-header { align-items: flex-start; flex-direction: column; }
    .trace-subtitle { display: none; }
    .transfer-card { width: 300px; flex-basis: 300px; }
  }
</style>
