<script>
  import { onDestroy, onMount } from 'svelte';
  import BottomLaunchHistory from './lib/components/BottomLaunchHistory.svelte';
  import EntitySummaryCard from './lib/components/EntitySummaryCard.svelte';
  import FooterBarcode from './lib/components/FooterBarcode.svelte';
  import GraphView from './lib/components/GraphView.svelte';
  import TopKpiStrip from './lib/components/TopKpiStrip.svelte';
  import TargetHistory from './lib/components/TargetHistory.svelte';
  import WalletInspector from './lib/components/WalletInspector.svelte';
  import { WS_URL, fetchCachedEntity, fetchState, scanEntity, trackEntity } from './lib/api.js';
  import { tokenLinks } from './lib/externalLinks.js';

  let searchQuery = $state('');
  let scanDepth = $state(20);
  let scanning = $state(false);
  let tracking = $state(false);
  let selectedWallet = $state.raw(null);
  let state = $state.raw(null);
  let analysis = $state.raw(null);
  let errorMessage = $state('');
  let statusMessage = $state('');
  let wsConnected = $state(false);
  let socket = null;
  let reconnectTimer = null;
  let cacheRefreshTimer = null;
  let initialStateTimer = null;
  let initialStateReceived = false;
  let destroyed = false;

  function isoTimestamp(value) {
    if (value === null || value === undefined || value === '') return null;
    const numericValue = typeof value === 'number' ? value : Number(value);
    const date = Number.isFinite(numericValue)
      ? new Date(numericValue > 100_000_000_000 ? numericValue : numericValue * 1000)
      : new Date(value);
    return Number.isNaN(date.getTime()) ? null : date.toISOString();
  }

  const recentTokens = $derived.by(() => {
    const scanned = [
      ...(analysis?.entity_mints || []),
      ...(analysis?.linked_launches || []),
      ...(analysis?.launches || []),
    ];
    const source = analysis ? scanned : (state?.launches || []);
    const tokens = [...new Map(source.map((launch) => [launch.mint, launch])).values()]
      .map((launch) => ({
        mint: launch.mint,
        symbol: launch.symbol || null,
        name: launch.name || null,
        creator: launch.creator || launch.creator_wallet || null,
        launch_time: isoTimestamp(launch.created_at),
        status: launch.status || 'FINALIZED',
        isCreationEvent: true,
        relation: launch.relation || null,
        links: tokenLinks(launch.mint, launch.bonding_curve),
      }));
    const identity = analysis?.identity;
    if (!identity?.is_token || !identity.input) return tokens;
    if (tokens.some((token) => token.mint === identity.input)) return tokens;
    return [
      {
        mint: identity.input,
        symbol: identity.token_symbol || null,
        name: identity.token_name || null,
        creator: identity.resolved_creator || null,
        launch_time: null,
        status: 'RESOLVED TARGET',
        isCreationEvent: false,
        links: tokenLinks(identity.input, identity.bonding_curve),
      },
      ...tokens,
    ];
  });
  const finalizedLaunchCount = $derived(
    recentTokens.filter((token) => token.isCreationEvent).length,
  );
  const graphNodeCount = $derived(analysis?.graph?.nodes?.length || 0);
  const repeatBundlerCount = $derived(
    new Set(
      (analysis?.repeat_bundler_entities || [])
        .map((row) => row.bundler_wallet)
        .filter(Boolean),
    ).size,
  );
  const evidence = $derived.by(() => {
    if (!analysis) return [];
    return [
      `Finalized launches: ${analysis.rug_evidence?.launch_count || 0}`,
      `Linked finalized launches: ${analysis.rug_evidence?.linked_launch_count || 0}`,
      `Repeat bundler mints: ${analysis.rug_evidence?.repeat_bundler_mint_count || 0}`,
      `Entity-wide finalized mints: ${analysis.entity_mint_discovery?.finalized_mint_count || 0}`,
      ...(analysis.repeat_bundler_entities || []).map(
        (row) =>
          `Repeat bundler ${shortAddress(row.bundler_wallet)}: ${row.mint_count} creator mints`,
      ),
    ];
  });

  function shortAddress(address) {
    return address ? `${address.slice(0, 4)}...${address.slice(-4)}` : '—';
  }

  function formatSol(lamports) {
    return typeof lamports === 'number'
      ? `${(lamports / 1_000_000_000).toFixed(4)} SOL`
      : '—';
  }

  function selectAnalysisWallet(data) {
    const stats = data.stats || {};
    selectedWallet = {
      address: data.identity?.resolved_creator || data.target_wallet,
      role: data.identity?.is_token ? 'RESOLVED CREATOR' : 'SCANNED WALLET',
      balance: null,
      confidence: null,
      firstSeen: stats.first_seen_slot ? `slot ${stats.first_seen_slot}` : null,
      lastActive: stats.last_seen_slot ? `slot ${stats.last_seen_slot}` : null,
      inboundSol: formatSol(stats.native_in_lamports),
      outboundSol: formatSol(stats.native_out_lamports),
      totalLaunches:
        (data.rug_evidence?.launch_count || 0) +
        (data.rug_evidence?.linked_launch_count || 0),
      linkedWallets: Math.max(0, (data.graph?.nodes?.length || 1) - 1),
      transfers: data.transfers || [],
      canTrack: Boolean(data.tracking_address),
    };
  }

  async function loadData() {
    try {
      state = await fetchState();
      errorMessage = '';
    } catch (error) {
      errorMessage = error.message;
    }
  }

  async function handleSync() {
    await loadData();
    statusMessage = 'Data synchronized.';
  }

  async function refreshCachedAnalysis() {
    if (!analysis?.backfill?.resumable || !searchQuery) return;
    try {
      const response = await fetchCachedEntity(searchQuery.trim());
      if (response.data) {
        analysis = response.data;
        selectAnalysisWallet(response.data);
        statusMessage = 'Entity data refreshed.';
      }
      await loadData();
    } catch (error) {
      if (!String(error.message).includes('no cached entity report')) {
        errorMessage = error.message;
      }
    }
  }

  async function handleScan(targetAddress = null) {
    const query = (targetAddress || searchQuery).trim();
    if (!query || scanning) return;
    searchQuery = query;
    scanning = true;
    errorMessage = '';
    statusMessage = '';
    try {
      const response = await scanEntity(query, scanDepth);
      if (!response.ok || !response.data) {
        throw new Error(response.message || response.error || 'Finalized scan abstained');
      }
      analysis = response.data;
      selectAnalysisWallet(response.data);
      statusMessage = 'Entity data loaded.';
      await loadData();
    } catch (error) {
      errorMessage = error.message;
    } finally {
      scanning = false;
    }
  }

  function handleSelectWallet(wallet) {
    const node = analysis?.graph?.nodes?.find((item) => item.address === wallet.address);
    selectedWallet = {
      ...wallet,
      firstSeen: node?.first_seen_slot ? `slot ${node.first_seen_slot}` : null,
      lastActive: node?.last_seen_slot ? `slot ${node.last_seen_slot}` : null,
      totalLaunches: node?.launch_count ?? null,
      linkedWallets: null,
      transfers: (analysis?.transfers || []).filter(
        (row) => row.source === wallet.address || row.target === wallet.address,
      ),
      canTrack: wallet.address === analysis?.tracking_address,
    };
  }

  async function handleTrack() {
    if (!analysis?.tracking_address || tracking) return;
    tracking = true;
    errorMessage = '';
    try {
      const response = await trackEntity(analysis.tracking_address);
      state = response.state;
      statusMessage = response.message;
    } catch (error) {
      errorMessage = error.message;
    } finally {
      tracking = false;
    }
  }

  function connectEvents() {
    socket = new WebSocket(WS_URL);
    socket.onopen = () => (wsConnected = true);
    socket.onmessage = ({ data }) => {
      const event = JSON.parse(data);
      if (event.type === 'state') {
        state = event.data;
        initialStateReceived = true;
        if (initialStateTimer) clearTimeout(initialStateTimer);
      }
      if (event.type === 'launch_detected') {
        statusMessage = `New finalized launch: ${event.data?.data?.mint || event.data?.wallet}`;
        loadData();
      }
    };
    socket.onclose = () => {
      wsConnected = false;
      if (!destroyed) reconnectTimer = setTimeout(connectEvents, 3000);
    };
  }

  onMount(() => {
    connectEvents();
    initialStateTimer = setTimeout(() => {
      if (!initialStateReceived) loadData();
    }, 1200);
    cacheRefreshTimer = setInterval(refreshCachedAnalysis, 5000);
  });

  onDestroy(() => {
    destroyed = true;
    if (reconnectTimer) clearTimeout(reconnectTimer);
    if (initialStateTimer) clearTimeout(initialStateTimer);
    if (cacheRefreshTimer) clearInterval(cacheRefreshTimer);
    socket?.close();
  });
</script>

<div class="app-layout">
  <header class="technical-header">
    <div class="header-brand">
      <div class="brand-badge">RUGBOT // SPEC</div>
      <h1>TECHNICAL NOIR LABEL SYSTEM</h1>
    </div>
    <div class="header-search">
      <span>[ TARGET QUERY ]</span>
      <input
        type="text"
        placeholder="Paste root wallet / dev wallet / mint address"
        bind:value={searchQuery}
        onkeydown={(event) => event.key === 'Enter' && handleScan()}
      />
      <button onclick={() => searchQuery && navigator.clipboard.writeText(searchQuery)} title="Copy input">📋</button>
    </div>
    <div class="header-actions">
      <select bind:value={scanDepth} aria-label="Scan depth">
        <option value={20}>QUICK · 20 TX</option>
        <option value={100}>DEEP · 100 TX · HIGH-RATE RPC</option>
      </select>
      <button class="btn btn-hazard" onclick={() => handleScan()} disabled={scanning}>
        🔍 {scanning ? 'SCANNING ON-CHAIN...' : 'RESOLVE ENTITY'}
      </button>
      <button class="btn btn-outline" onclick={handleSync}>🔄 SYNC</button>
    </div>
  </header>

  {#if errorMessage}<div class="api-notice error">{errorMessage}</div>{/if}
  {#if statusMessage}<div class="api-notice success">{statusMessage}</div>{/if}
  <TopKpiStrip
    rootWallet={shortAddress(analysis?.identity?.resolved_creator)}
    rawAddress={analysis?.identity?.resolved_creator || ''}
    linkedWallets={Math.max(0, graphNodeCount - 1)}
    creatorWallets={new Set(recentTokens.map((token) => token.creator).filter(Boolean)).size}
    bundleWallets={repeatBundlerCount}
    fundingHubs={analysis?.identity?.root_funder ? 1 : 0}
    launchesCount={finalizedLaunchCount}
  />

  <main class="technical-grid">
    <aside class="entity-rail">
      <TargetHistory
        history={state?.target_history || []}
        onOpen={(item) => handleScan(item.query)}
      />
      <EntitySummaryCard
        clusterId={shortAddress(analysis?.tracking_address)}
        totalWallets={graphNodeCount}
        firstSeen={analysis?.stats?.first_seen_slot ? `slot ${analysis.stats.first_seen_slot}` : '—'}
        lastSeen={analysis?.stats?.last_seen_slot ? `slot ${analysis.stats.last_seen_slot}` : '—'}
        {evidence}
      />
    </aside>
    <section class="graph-pane">
      <GraphView entity={analysis} onSelectWallet={handleSelectWallet} />
    </section>
    <aside class="inspector-pane">
      <WalletInspector
        {selectedWallet}
        onTrack={handleTrack}
        onExpand={() => handleScan(selectedWallet?.address)}
      />
    </aside>
    <div class="history-pane">
      <BottomLaunchHistory tokens={recentTokens} />
    </div>
  </main>

  <FooterBarcode
    version="RUGBOT"
    buildId="SVELTE"
    network="MAINNET"
    rpcStatus={wsConnected ? 'CONNECTED' : 'DISCONNECTED'}
    jitoStatus="NOT CHECKED"
    radarStatus={state?.observation?.status || 'UNKNOWN'}
  />
</div>

<style>
  .app-layout { width: 100%; max-width: 1720px; margin: 0 auto; padding: 12px; display: flex; flex-direction: column; gap: 6px; }
  .technical-header { display: grid; grid-template-columns: auto minmax(360px, 1fr) auto; align-items: center; gap: 10px; padding: 8px 12px; background: var(--surface-container-lowest); border: 1.5px solid var(--outline-variant); }
  .header-brand, .header-actions { display: flex; align-items: center; gap: 7px; }
  .header-actions select { height: 27px; padding: 3px 6px; background: var(--surface-container-low); border: 1px solid var(--outline-variant); color: var(--on-surface); font: 9px var(--font-meta); }
  .brand-badge { padding: 2px 6px; background: var(--primary-container); color: #000; font: 800 10px var(--font-meta); }
  h1 { margin: 0; color: var(--stark-white); font: 900 16px var(--font-headline); letter-spacing: .05em; }
  .header-search { position: relative; width: 100%; display: flex; align-items: center; }
  .header-search span { align-self: stretch; display: flex; flex: 0 0 auto; align-items: center; padding: 0 8px; background: var(--surface-container-low); border: 1px solid var(--outline-variant); border-right: 0; color: var(--primary-container); font: 700 9px var(--font-meta); pointer-events: none; }
  .header-search input { width: 100%; min-width: 0; padding: 7px 32px 7px 8px; background: var(--surface-container-low); border: 1px solid var(--outline-variant); color: var(--on-surface); font: 12px var(--font-data); }
  .header-search button { position: absolute; right: 8px; padding: 0; background: none; border: 0; cursor: pointer; }
  .api-notice { padding: 6px 10px; border: 1px solid var(--outline-variant); font: 11px var(--font-data); }
  .api-notice.error { border-color: var(--hazard-red); color: var(--error); background: var(--error-container); }
  .api-notice.success { border-color: var(--green-ok); color: var(--green-ok); background: var(--green-container); }
  .technical-grid { display: grid; grid-template-columns: 260px minmax(0, 1fr) 280px; grid-template-areas: "rail graph inspector" "rail history history"; gap: 8px; align-items: start; }
  .entity-rail { grid-area: rail; min-width: 0; }
  .graph-pane { grid-area: graph; min-width: 0; }
  .inspector-pane { grid-area: inspector; min-width: 0; }
  .history-pane { grid-area: history; min-width: 0; }
  @media (max-width: 1200px) {
    .technical-header { grid-template-columns: auto 1fr; }
    .header-actions { grid-column: span 2; justify-content: flex-end; }
    .technical-grid { grid-template-columns: minmax(0, 1fr) 280px; grid-template-areas: "graph inspector" "rail rail" "history history"; }
  }
  @media (max-width: 760px) {
    .technical-header { grid-template-columns: 1fr; }
    .header-actions { grid-column: auto; justify-content: stretch; }
    .technical-grid { grid-template-columns: 1fr; grid-template-areas: "graph" "inspector" "rail" "history"; }
  }
</style>
