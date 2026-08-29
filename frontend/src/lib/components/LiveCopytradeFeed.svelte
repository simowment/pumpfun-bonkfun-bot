<script>
  import { getSortedCandidates, deriveFactualPills, shortAddr as shortAddrHelper } from "../copytradeEvidence.js";
  import { accountLinks } from "../externalLinks.js";

  let { liveLaunches = [], report = null, onInspect = null, onTrack = null } = $props();
  let ruggedOnly = $state(true);

  function shortAddr(a) {
    if (!a || a.length < 10) return a || "—";
    return `${a.slice(0, 4)}…${a.slice(-4)}`;
  }

  function formatBlockTime(ts) {
    if (ts == null || !Number.isFinite(Number(ts)) || Number(ts) <= 0) return "—";
    try {
      return new Date(Number(ts) * 1000).toISOString().replace("T", " ").slice(0, 19) + " UTC";
    } catch (_) {
      return "—";
    }
  }

  function formatReceived(ts) {
    if (!ts) return "";
    try {
      return new Date(ts).toLocaleTimeString();
    } catch (_) {
      return "";
    }
  }

  function copy(addr) {
    if (!addr) return;
    navigator.clipboard.writeText(addr);
  }

  function hasReportForMint(mint) {
    const mints = report?.entity_mints;
    if (!Array.isArray(mints) || !mint) return false;
    return mints.some((m) => m?.mint === mint);
  }

  let enrichedCandidates = $derived.by(() => {
    try {
      if (!report) return [];
      return getSortedCandidates(report);
    } catch (_) {
      return [];
    }
  });

  // Rugged + ATH proxy: peak_mult >=2.0 (+100% per bible) and staged dump / adverse
  let ruggedCandidateWallets = $derived.by(() => {
    try {
      const set = new Set();
      for (const c of enrichedCandidates) {
        const peak = c.bundleEntries?.[0] ? null : null;
        // use entity_mints ATH if available via report enrichment
        const hasStagedDump = c.dumpGroups?.some((g) => g.sellCount >= 2) || c.dumpGroups?.reduce((s,g)=>s+g.sellCount,0) >= 1;
        // also check pills: STAGED DUMP etc — derive via dumpGroups
        // ATH check via report entity_mints aligned by creator wallet
        const mints = report?.entity_mints ?? [];
        const related = mints.filter((m) => (m.creator_wallet ?? m.actor_wallet) === c.wallet || report?.tracking_address === c.wallet);
        const hasAth = related.some((m) => {
          const pm = m.peak_mult ?? (m.ath_multiplier != null ? Number(m.ath_multiplier) : null);
          return pm != null && Number(pm) >= 2.0;
        });
        // consider rugged if either staged dump OR ath+rugged (at least 2x)
        // bible: +100% amplitude + later rugged check; we require both signals when available
        if (hasAth && hasStagedDump) set.add(c.wallet);
        else if (hasStagedDump && related.length === 0) set.add(c.wallet); // dump alone counts as rugged
        else if (hasAth) set.add(c.wallet);
      }
      return set;
    } catch (_) { return new Set(); }
  });

  let visibleLaunches = $derived.by(() => {
    if (!ruggedOnly) return liveLaunches;
    if (!report || enrichedCandidates.length === 0) return liveLaunches; // fallback all when no enrichment yet
    // if ruggedCandidateWallets empty, fallback to all to avoid empty feed
    if (ruggedCandidateWallets.size === 0) return liveLaunches;
    // filter to mints that belong to rugged candidate wallets' bundle mints
    const ruggedMints = new Set();
    for (const c of enrichedCandidates) {
      if (!ruggedCandidateWallets.has(c.wallet)) continue;
      for (const e of c.bundleEntries ?? []) if (e.mint) ruggedMints.add(e.mint);
    }
    if (ruggedMints.size === 0) return liveLaunches;
    const filtered = liveLaunches.filter((l) => ruggedMints.has(l.mint));
    return filtered.length > 0 ? filtered : liveLaunches;
  });

  function pillClass(tone) {
    if (tone === "leader") return "chip leader";
    if (tone === "repeat") return "chip amber";
    if (tone === "dump") return "chip green";
    if (tone === "burner") return "chip dim";
    if (tone === "aborted") return "chip aborted";
    return "chip dim";
  }
</script>

<div class="live-feed-wrap">
  <div class="live-feed-head">
    <div class="live-title">
      <span class="pulse"></span>
      LIVE STREAM <span class="live-sub">PumpPortal finalized · observe only · bible ≤15k MC</span>
    </div>
    <label class="live-toggle" title="Filtre bible: B0 + ≤15k MC + ATH ≥2× + rugged">
      <input type="checkbox" bind:checked={ruggedOnly} />
      <span class="toggle-label">Rugged + ATH only</span>
    </label>
    <span class="live-count">{visibleLaunches.length} / {liveLaunches.length}</span>
  </div>

  {#if visibleLaunches.length === 0}
    <div class="live-empty">Aucun rugged bible match — bascule sur "all" pour voir le flux brut…</div>
  {:else}
    <div class="live-list">
      {#each visibleLaunches as launch (launch.signature + launch.mint)}
        {@const creatorLinks = accountLinks(launch.creator || "")}
        {@const mintShort = shortAddr(launch.mint)}
        {@const creatorShort = shortAddr(launch.creator)}
        {@const enriched = hasReportForMint(launch.mint)}
        {@const age = formatReceived(launch.receivedAt)}
        <div class="live-row" class:enriched>
          <div class="row-top">
            <span class="mint-pill" title={launch.mint}>{mintShort}</span>
            <button class="mini-copy" onclick={() => copy(launch.mint)} title="Copy mint">⧉</button>
            <a class="ext-mini" href={`https://solscan.io/token/${encodeURIComponent(launch.mint)}`} target="_blank" rel="noreferrer">SOLSCAN ↗</a>
            <a class="ext-mini" href={`https://pump.fun/coin/${encodeURIComponent(launch.mint)}`} target="_blank" rel="noreferrer">PUMP ↗</a>
            <span class="slot-label">slot {launch.slot ?? "—"}</span>
            <span class="time-label" title={formatBlockTime(launch.blockTime)}>{formatBlockTime(launch.blockTime)}</span>
            <span class="recv-label">{age}</span>
          </div>

          <div class="row-mid">
            <span class="creator-label">creator</span>
            <span class="mono-addr" title={launch.creator}>{creatorShort}</span>
            <button class="mini-copy" onclick={() => copy(launch.creator)} title="Copy creator">⧉ COPY</button>
            <a class="ext-mini" href={creatorLinks.solscan} target="_blank" rel="noreferrer">SOLSCAN ↗</a>
            <a class="ext-mini" href={creatorLinks.gmgn} target="_blank" rel="noreferrer">GMGN ↗</a>
            {#if launch.signature}
              <span class="sig-label" title={launch.signature}>{shortAddr(launch.signature)}</span>
            {/if}
          </div>

          {#if enriched && enrichedCandidates.length > 0}
            {@const top = enrichedCandidates[0]}
            {@const pills = deriveFactualPills(top)}
            <div class="row-pills">
              {#each pills as p}
                <span class={pillClass(p.tone)}>{p.label}</span>
              {/each}
              <span class="chip dim">{enrichedCandidates.length} CANDIDATE{enrichedCandidates.length !== 1 ? "S" : ""}</span>
              <span class="chip dim">{top.bundledMintCount} MINT{top.bundledMintCount !== 1 ? "S" : ""}</span>
              {#if top.bundleEntries?.some((e) => e.entryBlock === "B0")}
                <span class="chip leader">B0</span>
              {/if}
              {#if top.bundleEntries?.every((e) => e.token_amount === 0 || e.token_amount == null) && top.bundleEntries.length > 0}
                <span class="chip aborted">ABORTED</span>
              {/if}
            </div>
            <div class="enriched-hint">Bundle loaded for this mint — see COPYTRADE tab for full evidence</div>
          {:else if enriched && enrichedCandidates.length === 0}
            <div class="row-pills">
              <span class="chip aborted">ABORTED</span>
              <span class="chip dim">NO BUNDLE</span>
            </div>
            <div class="enriched-hint">No bundle buys for this entity — fail-closed</div>
          {:else}
            <div class="row-pills">
              <span class="chip dim">SCAN TO REVEAL BUNDLE</span>
            </div>
            <div class="scan-hint">Scan to reveal bundle — fail-closed, pas de guess</div>
          {/if}

          <div class="row-actions">
            <button class="act-btn primary" onclick={() => onInspect && onInspect(launch.mint)} title="Scan mint and open copytrade">
              ◈ Inspect
            </button>
            <button class="act-btn" onclick={() => onTrack && onTrack(launch.creator)} title="Track creator wallet">Track</button>
            <span class="observe-only">observe only · no autobuy</span>
          </div>
        </div>
      {/each}
    </div>
  {/if}
</div>

<style>
  .live-feed-wrap {
    display: flex;
    flex-direction: column;
    gap: 0;
    background: var(--surface-container-lowest);
    border: 1px solid var(--outline-variant);
  }
  .live-feed-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 7px 10px;
    background: var(--surface-container-low);
    border-bottom: 1px solid var(--outline-variant);
  }
  .live-title {
    display: flex;
    align-items: center;
    gap: 7px;
    color: var(--stark-white);
    font: 800 11px var(--font-mono);
    letter-spacing: 0.06em;
  }
  .live-sub {
    color: var(--on-surface-variant);
    font: 400 9px var(--font-mono);
    letter-spacing: 0.04em;
  }
  .pulse {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--green-ok);
    box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.45);
    animation: pulse 1.6s infinite;
  }
  @keyframes pulse {
    0% { box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.45); }
    70% { box-shadow: 0 0 0 6px rgba(16, 185, 129, 0); }
    100% { box-shadow: 0 0 0 0 rgba(16, 185, 129, 0); }
  }
  .live-toggle {
    display: flex;
    align-items: center;
    gap: 4px;
    color: var(--on-surface-variant);
    font: 700 9px var(--font-mono);
    cursor: pointer;
  }
  .live-toggle input { accent-color: var(--primary-container); }
  .toggle-label { letter-spacing: 0.04em; }
  .live-count {
    color: var(--on-surface-variant);
    font: 700 10px var(--font-mono);
  }
  .live-empty {
    padding: 28px 16px;
    text-align: center;
    color: var(--on-surface-variant);
    font: 400 12px var(--font-sans);
    background: var(--surface-container-low);
    border-top: 1px dashed var(--outline-variant);
  }
  .live-list {
    display: flex;
    flex-direction: column;
    max-height: 420px;
    overflow-y: auto;
    scroll-behavior: smooth;
  }
  .live-row {
    display: flex;
    flex-direction: column;
    gap: 5px;
    padding: 9px 10px;
    border-bottom: 1px solid var(--outline-variant);
    background: var(--surface-container-lowest);
  }
  .live-row:hover {
    background: var(--surface-container-low);
  }
  .live-row.enriched {
    border-left: 2px solid var(--primary-container);
  }
  .row-top, .row-mid {
    display: flex;
    align-items: center;
    gap: 6px;
    flex-wrap: wrap;
  }
  .mint-pill {
    padding: 2px 6px;
    background: var(--primary-container);
    color: #000;
    border: 1px solid var(--stark-white);
    font: 800 11px var(--font-mono);
    letter-spacing: 0.02em;
  }
  .mono-addr, .sig-label {
    font: 400 11px var(--font-mono);
    color: var(--on-surface);
  }
  .slot-label, .time-label, .recv-label, .creator-label {
    color: var(--on-surface-variant);
    font: 400 10px var(--font-mono);
  }
  .creator-label {
    font: 700 9px var(--font-mono);
    letter-spacing: 0.05em;
    text-transform: uppercase;
  }
  .mini-copy {
    padding: 2px 5px;
    background: var(--surface-container-high);
    border: 1px solid var(--outline-variant);
    color: var(--on-surface);
    font: 700 9px var(--font-mono);
    cursor: pointer;
  }
  .mini-copy:hover {
    border-color: var(--primary-container);
    color: var(--primary-container);
  }
  .ext-mini {
    padding: 2px 5px;
    border: 1px solid var(--outline-variant);
    background: var(--surface-container-high);
    color: var(--on-surface);
    text-decoration: none;
    font: 700 9px var(--font-mono);
  }
  .ext-mini:hover {
    border-color: var(--tertiary);
    color: var(--tertiary);
  }
  .row-pills {
    display: flex;
    gap: 4px;
    flex-wrap: wrap;
    margin-top: 2px;
  }
  .chip {
    padding: 2px 5px;
    border: 1px solid var(--outline-variant);
    background: var(--surface-container);
    color: var(--on-surface);
    font: 700 8px var(--font-mono);
    letter-spacing: 0.03em;
  }
  .chip.dim { color: var(--on-surface-variant); }
  .chip.amber { border-color: var(--primary-container); color: var(--primary-container); background: rgba(255, 140, 0, 0.12); }
  .chip.green { border-color: var(--green-ok); color: var(--green-ok); background: rgba(16, 185, 129, 0.12); }
  .chip.leader { border-color: var(--tertiary); color: var(--tertiary); background: rgba(96, 197, 255, 0.12); }
  .chip.aborted { border-color: var(--hazard-red); color: var(--error); background: rgba(92, 20, 24, 0.35); }
  .enriched-hint, .scan-hint {
    color: var(--on-surface-variant);
    font: 400 9px var(--font-mono);
    letter-spacing: 0.02em;
  }
  .scan-hint { color: var(--on-surface-variant); font-style: italic; }
  .row-actions {
    display: flex;
    align-items: center;
    gap: 6px;
    margin-top: 2px;
  }
  .act-btn {
    padding: 4px 10px;
    background: var(--surface-container-high);
    border: 1px solid var(--outline-variant);
    color: var(--on-surface);
    font: 700 10px var(--font-mono);
    cursor: pointer;
  }
  .act-btn:hover { border-color: var(--primary-container); color: var(--primary-container); }
  .act-btn.primary {
    background: var(--primary-container);
    color: #000;
    border-color: var(--stark-white);
  }
  .act-btn.primary:hover { filter: brightness(1.05); }
  .observe-only {
    margin-left: auto;
    color: var(--on-surface-variant);
    font: 400 9px var(--font-mono);
    letter-spacing: 0.04em;
  }
</style>
