<script>
  import { onMount, onDestroy } from 'svelte';
  import TopKpiStrip from './lib/components/TopKpiStrip.svelte';
  import EntitySummaryCard from './lib/components/EntitySummaryCard.svelte';
  import GraphView from './lib/components/GraphView.svelte';
  import BottomLaunchHistory from './lib/components/BottomLaunchHistory.svelte';
  import WalletInspector from './lib/components/WalletInspector.svelte';
  import FooterBarcode from './lib/components/FooterBarcode.svelte';

  import {
    API_BASE,
    WS_URL,
    fetchState,
    scanEntity,
    fetchCachedEntity,
    fetchEntityScanHistory,
    fetchWalletBalance,
    trackEntity,
    fetchRuggers,
  } from './lib/api.js';

  let searchQuery = $state('');
  let scanning = $state(false);
  let tracking = $state(false);
  let selectedWallet = $state(null);
  let entityScanHistory = $state([]);
  let historyLoading = $state(false);
  let historyError = $state('');
  let activeBottomTab = $state('launches');

  let state = $state(null);
  let analysis = $state(null);
  let wsConn = $state(false);
  let wsRef = null;
  let statusMessage = $state('');
  let liveLaunches = $state([]);

  // Rugger ranking (§14 evidence), served from the headless discover DB.
  let ruggers = $state([]);
  let ruggersLoading = $state(false);
  let ruggersError = $state('');
  let ruggersType2Note = $state('');

  function shortAddr(a) {
    if (!a || a.length < 10) return a || '—';
    return `${a.slice(0, 4)}...${a.slice(-4)}`;
  }

  // Derive dynamic real tokens from analysis report or state launches
  const graphNodes = $derived(analysis?.graph?.nodes ?? []);
  function roleCount(role) {
    return graphNodes.filter((n) => (n.roles || []).includes(role)).length;
  }
  // created_at is seconds on own launches and milliseconds on linked mints.
  function launchTime(createdAt) {
    if (!createdAt) return null;
    const ms = createdAt > 1e12 ? createdAt : createdAt * 1000;
    return new Date(ms).toISOString().replace('T', ' ').slice(0, 19) + ' UTC';
  }

  function tokenRow(item, own) {
    const bundleWallets = (item.bundle_buys || []).length;
    return {
      mint: item.mint,
      symbol: item.symbol || shortAddr(item.mint),
      name: item.name || '',
      action: own ? 'LAUNCHED' : 'LINKED LAUNCH',
      action_type: 'launch',
      actor_wallet: item.creator,
      actor_role: own ? 'Scanned creator' : 'Linked creator',
      creator: item.creator,
      launch_time: launchTime(item.created_at),
      bundle_wallets: own ? null : bundleWallets,
      early_position: item.position_is_zero_or_one,
      links: {
        pumpfun: `https://pump.fun/coin/${item.mint}`,
        solscan: `https://solscan.io/token/${item.mint}`,
      },
    };
  }

  // The scanned wallet's own launches plus launches by linked creator wallets.
  const dynamicTokens = $derived.by(() => {
    const rows = new Map();
    for (const launch of analysis?.launches ?? []) rows.set(launch.mint, tokenRow(launch, true));
    for (const mint of analysis?.entity_mints ?? []) {
      if (!rows.has(mint.mint)) rows.set(mint.mint, tokenRow(mint, false));
    }
    if (rows.size === 0) {
      for (const launch of state?.launches ?? []) rows.set(launch.mint, tokenRow(launch, true));
    }
    return [...rows.values()].sort((a, b) => (b.launch_time || '').localeCompare(a.launch_time || ''));
  });

  async function loadEntityScanHistory(address) {
    const normalizedAddress = (address || '').trim();
    if (!normalizedAddress) {
      entityScanHistory = [];
      historyError = '';
      return;
    }
    historyLoading = true;
    historyError = '';
    try {
      const response = await fetchEntityScanHistory(normalizedAddress, 100);
      entityScanHistory = response?.scans ?? [];
    } catch (error) {
      entityScanHistory = [];
      historyError = error.message;
    } finally {
      historyLoading = false;
    }
  }

  async function loadData() {
    try {
      state = await fetchState();
      const latestQuery = searchQuery || state?.target_history?.[0]?.query || '';
      if (!latestQuery) return false;

      searchQuery = latestQuery.trim();
      return await loadCachedAnalysis(searchQuery);
    } catch (e) {
      console.error('Data load error:', e);
      return false;
    }
  }

  function updateSelectedFromAnalysis(data) {
    const trackingAddr = data?.tracking_address || data?.identity?.input || searchQuery;
    selectedWallet = {
      address: trackingAddr,
      role: data?.identity?.is_token ? 'Resolved Creator' : 'Master Deployer',
      balance: '—',
      confidence: data?.confidence != null ? `${(data.confidence * 100).toFixed(1)}%` : '—',
      firstSeen: data?.stats?.first_seen_slot ? `Slot ${data.stats.first_seen_slot}` : '—',
      lastActive: data?.stats?.last_seen_slot ? `Slot ${data.stats.last_seen_slot}` : '—',
      inboundSol: data?.stats?.native_in_lamports != null ? `${(data.stats.native_in_lamports / 1e9).toFixed(2)} SOL` : '—',
      outboundSol: data?.stats?.native_out_lamports != null ? `${(data.stats.native_out_lamports / 1e9).toFixed(2)} SOL` : '—',
      totalLaunches: data?.entity_mints?.length ?? 0,
      linkedWallets: data?.graph?.nodes?.length ? data.graph.nodes.length - 1 : 0,
    };
    fetchWalletBalance(trackingAddr).then((b) => {
      if (b?.data?.balance_lamports !== undefined) {
        selectedWallet.balance = `${(b.data.balance_lamports / 1e9).toFixed(4)} SOL`;
      }
    }).catch(() => {});
  }

  async function loadCachedAnalysis(query) {
    try {
      const cached = await fetchCachedEntity(query);
      if (!cached?.data) return false;
      analysis = cached.data;
      updateSelectedFromAnalysis(cached.data);
      await loadEntityScanHistory(cached.data.tracking_address || query);
      return true;
    } catch (_) {
      return false;
    }
  }

  const BACKFILL_POLL_MS = 3000;
  const BACKFILL_POLL_LIMIT = 60;

  // A scan returns before its history backfill finishes; re-read the cached
  // report until the backfill reports complete so linked launches appear.
  async function followBackfill(query, polls = 0) {
    if (analysis?.backfill?.status === 'complete' || polls >= BACKFILL_POLL_LIMIT) return;
    await new Promise((resolve) => setTimeout(resolve, BACKFILL_POLL_MS));
    if (searchQuery.trim() !== query) return;
    try {
      const cached = await fetchCachedEntity(query);
      if (cached?.data) analysis = cached.data;
    } catch {
      // The report is not cached until the first backfill page is written.
    }
    await followBackfill(query, polls + 1);
  }

  async function handleScan(targetAddr = null) {
    const query = (targetAddr || searchQuery).trim();
    if (!query) return;
    searchQuery = query;
    scanning = true;
    statusMessage = 'Scanning on-chain entity...';
    try {
      const res = await scanEntity(query, 100);
      if (res?.ok && res.data) {
        analysis = res.data;
        updateSelectedFromAnalysis(res.data);
        await loadEntityScanHistory(res.data.tracking_address || query);
        followBackfill(query);
        activeBottomTab = 'scans';
        statusMessage = `Discovered on-chain entity: ${shortAddr(res.data.tracking_address || query)}`;
      } else {
        const loaded = await loadCachedAnalysis(query);
        await loadEntityScanHistory(
          loaded ? analysis.tracking_address || query : query
        );
        activeBottomTab = 'scans';
        statusMessage = loaded
          ? `Showing cached finalized analysis: ${res?.message || 'new scan unavailable'}`
          : res?.message || 'Scan completed.';
      }
    } catch (e) {
      const loaded = await loadCachedAnalysis(query);
      await loadEntityScanHistory(
        loaded ? analysis.tracking_address || query : query
      );
      activeBottomTab = 'scans';
      statusMessage = loaded
        ? `Showing cached finalized analysis: ${e.message}`
        : `Scan error: ${e.message}`;
    } finally {
      scanning = false;
    }
  }

  async function handleTrackWallet() {
    if (!selectedWallet?.address || tracking) return;
    tracking = true;
    try {
      const res = await trackEntity(selectedWallet.address);
      if (res?.ok) {
        state = res.state;
        alert(`Watching ${shortAddr(selectedWallet.address)} in real-time observation mode.`);
      }
    } catch (e) {
      alert(`Tracking failed: ${e.message}`);
    } finally {
      tracking = false;
    }
  }

  function handleSelectWallet(w) {
    selectedWallet = {
      address: w.address,
      role: w.role || 'Cluster Node',
      balance: `${w.balance || '0.00'} SOL`,
      confidence: w.confidence || '—',
      firstSeen: '—',
      lastActive: '—',
      inboundSol: '—',
      outboundSol: '—',
      totalLaunches: dynamicTokens.length,
      linkedWallets: analysis?.graph?.nodes?.length || 0,
    };
    loadEntityScanHistory(w.address);
    fetchWalletBalance(w.address).then((b) => {
      if (b?.data?.balance_lamports !== undefined) {
        selectedWallet.balance = `${(b.data.balance_lamports / 1e9).toFixed(4)} SOL`;
      }
    }).catch(() => {});
  }

  function pushLiveLaunch(raw) {
    try {
      const outer = raw?.data ?? raw;
      // TrackerEvent jsonable shape: {event_type, root_funder, wallet, timestamp, data:{mint, signature, slot, created_at, creator...}}
      const inner = outer?.data && typeof outer.data === 'object' && ('mint' in outer.data || 'signature' in outer.data) ? outer.data : outer;
      const mint = inner?.mint ?? outer?.mint ?? null;
      if (!mint) return;
      const creator = inner?.creator ?? outer?.creator ?? outer?.wallet ?? raw?.wallet ?? null;
      const signature = inner?.signature ?? outer?.signature ?? outer?.created_signature ?? null;
      const slot = inner?.slot ?? outer?.slot ?? outer?.created_slot ?? null;
      const blockTime = inner?.created_at ?? outer?.created_at ?? inner?.timestamp ?? outer?.timestamp ?? null;
      const entry = {
        mint: String(mint),
        creator: creator ? String(creator) : '',
        signature: signature ? String(signature) : `${mint}:${slot ?? Date.now()}`,
        slot: slot != null ? Number(slot) : null,
        blockTime: blockTime != null ? Number(blockTime) : null,
        receivedAt: Date.now(),
      };
      // dedupe by signature+mint
      const exists = liveLaunches.some((l) => l.signature === entry.signature && l.mint === entry.mint);
      if (exists) return;
      liveLaunches = [entry, ...liveLaunches].slice(0, 100);
    } catch (_) {}
  }

  function initWebSocket() {
    try {
      wsRef = new WebSocket(WS_URL);
      wsRef.onopen = () => (wsConn = true);
      wsRef.onmessage = ({ data }) => {
        try {
          const ev = JSON.parse(data);
          if (ev.type === 'state') {
            state = ev.data;
            return;
          }
          if (ev.type === 'launch_detected') {
            pushLiveLaunch(ev);
            return;
          }
          if (ev.type === 'copytrade_candidate') {
            // future: treat as launch-like, allow feed to show enriched hint
            pushLiveLaunch(ev);
            return;
          }
          // fallback: some deployments send event_type field inside data
          const fallbackType = ev?.data?.event_type ?? ev?.event_type;
          if (fallbackType === 'launch_detected' || fallbackType === 'copytrade_candidate') {
            pushLiveLaunch(ev);
          }
        } catch (_) {}
      };
      wsRef.onclose = () => {
        wsConn = false;
        setTimeout(initWebSocket, 3000);
      };
    } catch (_) {
      wsConn = false;
    }
  }

  function handleLiveInspect(mint) {
    if (!mint) return;
    activeBottomTab = 'copytrade';
    handleScan(mint);
  }

  async function handleLiveTrack(creator) {
    if (!creator) return;
    try {
      const res = await trackEntity(creator);
      if (res?.ok) {
        state = res.state;
        statusMessage = `Tracking ${shortAddr(creator)} in observe-only mode`;
      }
    } catch (e) {
      statusMessage = `Track failed: ${e.message}`;
    }
  }

  async function loadRuggers() {
    ruggersLoading = true;
    ruggersError = '';
    try {
      const res = await fetchRuggers('30d', 2, 50);
      if (res?.ok) {
        ruggers = res.ruggers ?? [];
        ruggersType2Note = res.type2_note ?? '';
      } else {
        ruggers = [];
        ruggersError = res?.error || 'discover ruggers request failed';
      }
    } catch (e) {
      ruggers = [];
      ruggersError = e?.message || String(e);
    } finally {
      ruggersLoading = false;
    }
  }

  onMount(async () => {
    initWebSocket();
    loadRuggers();
    const urlTarget = new URLSearchParams(window.location.search).get('target');
    if (urlTarget) {
      searchQuery = urlTarget.trim();
    }
    const restored = await loadData();
    if (searchQuery && !restored) {
      handleScan();
    }
  });

  onDestroy(() => {
    if (wsRef) wsRef.close();
  });
</script>

<div class="app-layout">
  <!-- Top Global Header -->
  <header class="app-header">
    <div class="header-brand">
      <h1 class="brand-title">RUGBOT OPERATOR TRACKER</h1>
      <span class="live-pill">LIVE MAINNET</span>
    </div>

    <div class="header-search">
      <span class="search-label">Target:</span>
      <input
        type="text"
        placeholder="Enter dev wallet or token mint address..."
        bind:value={searchQuery}
        onkeydown={(e) => e.key === 'Enter' && handleScan()}
      />
      <button class="search-copy-btn" onclick={() => navigator.clipboard.writeText(searchQuery)} title="Copy address">
        ⧉
      </button>
    </div>

    <div class="header-actions">
      <button class="btn btn-hazard" onclick={() => handleScan()} disabled={scanning}>
        <span>🔍</span> {scanning ? 'Scanning...' : 'Scan Address'}
      </button>
      <button class="btn btn-outline" onclick={() => { loadData(); loadRuggers(); }}>
        <span>🔄</span> Sync
      </button>
    </div>
  </header>

  <!-- Top KPI 8-Column Strip -->
  <TopKpiStrip
    rootWallet={shortAddr(selectedWallet?.address || searchQuery)}
    rawAddress={selectedWallet?.address || searchQuery}
    linkedWallets={analysis?.stats?.direct_linked_wallet_count ?? Math.max((analysis?.graph?.nodes?.length || 1) - 1, 0)}
    creatorWallets={analysis?.stats?.linked_creator_wallet_count ?? (roleCount('MASTER_DEPLOYER') + roleCount('SUB_DEPLOYER'))}
    bundleWallets={analysis?.stats?.repeat_bundler_entity_count ?? roleCount('SATELLITE_BUNDLER')}
    fundingHubs={roleCount('FUNDER_HUB') + roleCount('funding_source')}
    launchesCount={analysis?.stats?.launch_count ?? dynamicTokens.length}
  />

  <!-- Main 3-Column Layout -->
  <main class="dashboard-grid">
    <!-- Left Column (Entity Summary & Evidence) -->
    <aside class="left-column">
      <EntitySummaryCard
        report={analysis}
        clusterId={`CL-${shortAddr(selectedWallet?.address || searchQuery)}`}
      />
    </aside>

    <!-- Center Column (Entity Bubble Map Canvas & Bottom Launch History) -->
    <section class="center-column">
      <GraphView
        cluster={analysis}
        entity={analysis}
        onSelectWallet={handleSelectWallet}
      />

      <BottomLaunchHistory
        tokens={dynamicTokens}
        targetHistory={state?.target_history ?? []}
        entityScanHistory={entityScanHistory}
        historyAddress={analysis?.tracking_address || selectedWallet?.address || searchQuery}
        historyLoading={historyLoading}
        historyError={historyError}
        report={analysis}
        activeTab={activeBottomTab}
        onTabChange={(tab) => (activeBottomTab = tab)}
        onSelectTarget={(target) => handleScan(target)}
        liveLaunches={liveLaunches}
        onLiveInspect={handleLiveInspect}
        onLiveTrack={handleLiveTrack}
        ruggers={ruggers}
        ruggersLoading={ruggersLoading}
        ruggersError={ruggersError}
        type2Note={ruggersType2Note}
      />
    </section>

    <!-- Right Column (Selected Wallet Inspector) -->
    <aside class="right-column">
      <WalletInspector
        {selectedWallet}
        onTrack={handleTrackWallet}
        onExpand={() => handleScan(selectedWallet?.address)}
      />
    </aside>
  </main>

  <!-- Bottom Barcode & Status Footer -->
  <FooterBarcode
    version="v2.0.5"
    buildId="Dynamic Mainnet Engine"
    network="Mainnet-Beta"
    rpcStatus={wsConn ? 'Online' : 'Connected'}
    jitoStatus="Operational"
  />
</div>

<style>
  .app-layout {
    width: 100%;
    max-width: 1740px;
    margin: 0 auto;
    padding: 12px;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .app-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 8px 14px;
    background-color: var(--surface-container-lowest);
    border: 1px solid var(--outline-variant);
    flex-wrap: wrap;
    gap: 10px;
  }
  .header-brand {
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .brand-title {
    font-family: var(--font-sans);
    font-size: 17px;
    font-weight: 800;
    letter-spacing: -0.01em;
    color: var(--stark-white);
    margin: 0;
  }
  .live-pill {
    background-color: var(--green-container);
    color: var(--green-ok);
    font-size: 11px;
    font-weight: 700;
    padding: 2px 7px;
    border: 1px solid var(--green-ok);
    border-radius: 3px;
  }
  .header-search {
    flex: 1;
    max-width: 540px;
    display: flex;
    align-items: center;
    position: relative;
  }
  .search-label {
    position: absolute;
    left: 10px;
    font-family: var(--font-sans);
    font-size: 12.5px;
    font-weight: 600;
    color: var(--primary-container);
    pointer-events: none;
  }
  .header-search input {
    width: 100%;
    background-color: var(--surface-container-low);
    border: 1px solid var(--outline-variant);
    padding: 7px 32px 7px 62px;
    font-family: var(--font-mono);
    font-size: 13px;
    color: var(--on-surface);
    outline: none;
    border-radius: 3px;
  }
  .header-search input:focus {
    border-color: var(--primary-container);
  }
  .search-copy-btn {
    position: absolute;
    right: 8px;
    background: none;
    border: none;
    cursor: pointer;
    font-size: 13px;
    color: var(--on-surface-variant);
  }
  .search-copy-btn:hover {
    color: var(--primary-container);
  }
  .header-actions {
    display: flex;
    gap: 6px;
    align-items: center;
  }
  .dashboard-grid {
    display: grid;
    grid-template-columns: 290px 1fr 310px;
    gap: 8px;
    align-items: start;
  }
  @media (max-width: 1400px) {
    .dashboard-grid {
      grid-template-columns: 280px 1fr;
    }
    .right-column {
      grid-column: span 2;
    }
  }
  @media (max-width: 900px) {
    .dashboard-grid {
      grid-template-columns: 1fr;
    }
  }
  .left-column {
    display: flex;
    flex-direction: column;
  }
  .center-column {
    display: flex;
    flex-direction: column;
    min-width: 0;
  }
  .right-column {
    display: flex;
    flex-direction: column;
  }
</style>
