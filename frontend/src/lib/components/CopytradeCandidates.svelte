<script>
  import { getSortedCandidates, formatSol, shortAddr, deriveFactualPills, deriveWhyLine, detectDegenerateState } from "../copytradeEvidence.js";
  import { accountLinks } from "../externalLinks.js";

  let { report = null } = $props();

  const candidates = $derived(getSortedCandidates(report));
  const degenerate = $derived(detectDegenerateState(report, candidates));

  let copiedAddr = $state(null);
  let expandedDetails = $state(new Set());
  let expandedBaskets = $state(new Set());

  function copy(addr) {
    if (!addr) return;
    navigator.clipboard.writeText(addr);
    copiedAddr = addr;
    setTimeout(() => (copiedAddr = null), 1800);
  }

  function toggleDetails(addr) {
    const next = new Set(expandedDetails);
    if (next.has(addr)) next.delete(addr);
    else next.add(addr);
    expandedDetails = next;
  }

  function toggleBasket(addr) {
    const next = new Set(expandedBaskets);
    if (next.has(addr)) next.delete(addr);
    else next.add(addr);
    expandedBaskets = next;
  }

  function pillClass(tone) {
    if (tone === "leader") return "chip leader";
    if (tone === "repeat") return "chip amber";
    if (tone === "dump") return "chip green";
    if (tone === "burner") return "chip dim";
    if (tone === "aborted") return "chip aborted";
    return "chip dim";
  }
</script>

<div class="copytrade-wrap">
  {#if !report}
    <div class="empty">No finalized report loaded — run a scan to derive candidates.</div>
  {:else}
    <!-- Clear Candidates header -->
    <div class="clear-head">
      <div class="clear-title">
        <span class="clear-icon">◈</span>
        CLEAR CANDIDATES
        <span class="clear-sub">RANKED FOR COPY · FACTUAL BADGES ONLY</span>
      </div>
      <div class="clear-meta">
        {#if candidates.length}
          <span>{candidates.length} CANDIDATE{candidates.length !== 1 ? "S" : ""} · SORTED BY EVIDENCE DENSITY</span>
          <span class="hint">B0 = same slot as creator · rank 1 = earliest in bundle · tap address to copy</span>
        {:else}
          <span>NO BUNDLE CANDIDATES DETECTED</span>
          <span class="hint">Finalized bundle buys empty — check degenerate explainer below</span>
        {/if}
      </div>
    </div>

    {#if degenerate.isDegenerate}
      <div class="degenerate-banner" class:warn={degenerate.allZeroTokens || degenerate.funderCount > 0}>
        <span class="warn-icon">⚠</span>
        <div class="warn-text">
          <b>{degenerate.message || "NO BUNDLE BUYS — degenerate bundle"}</b>
          <span>
            {#if degenerate.allZeroTokens}
              Fund-then-refund sweep — 0 tokens held at execution. No copyable buy to mirror.
            {:else if candidates.length === 0}
              No finalized bundle wallets found in creation slot. Creator is only persistent address.
            {:else}
              Bundle entries carry 0 tokens or empty — no executable buy size.
            {/if}
          </span>
        </div>
        {#if degenerate.creatorWallet}
          {@const clinks = accountLinks(degenerate.creatorWallet)}
          <div class="warn-actions">
            <span class="warn-label">PERSISTENT OPERATOR</span>
            <span class="mono-full">{degenerate.creatorWallet}</span>
            <button class="copy-btn primary" onclick={() => copy(degenerate.creatorWallet)}>{copiedAddr === degenerate.creatorWallet ? "✓ COPIED" : "⧉ COPY CREATOR"}</button>
            <a class="ext" href={clinks.solscan} target="_blank" rel="noreferrer">SOLSCAN ↗</a>
            <a class="ext" href={clinks.gmgn} target="_blank" rel="noreferrer">GMGN ↗</a>
          </div>
        {/if}
      </div>
      {#if candidates.length === 0 || degenerate.allZeroTokens}
        <div class="empty degenerate-empty">
          <b>No executable copytrade wallet — creator is only persistent address</b>
          <span>
            {#if degenerate.hasCreatorSell}
              Creator wallet sold 412k+ tokens with no bundle buys to copy. Mirroring would buy at no validated entry.
            {:else}
              No repeat bundler wallet with executable size. Use "NO COPY" — monitor creator for next launch only.
            {/if}
          </span>
          {#if candidates.length === 0 && !degenerate.creatorWallet}
            <span class="hint">If a repeat wallet appears in future scans, it will rank as ★ RECOMMENDED above.</span>
          {/if}
        </div>
      {/if}
    {/if}

    {#if candidates.length === 0}
      {#if !degenerate.isDegenerate}
        <div class="empty">No finalized bundle evidence — no copytrade candidates derived.</div>
      {/if}
    {:else}
      <div class="candidate-list">
        {#each candidates as c, idx (c.wallet)}
          {@const rank = idx + 1}
          {@const links = accountLinks(c.wallet)}
          {@const pills = deriveFactualPills(c)}
          {@const why = deriveWhyLine(c)}
          {@const isRecommended = rank === 1 && !degenerate.allZeroTokens}
          {@const isDimmed = rank > 1 || degenerate.allZeroTokens}
          <section class="candidate" class:recommended={isRecommended} class:dimmed={isDimmed} class:aborted={degenerate.allZeroTokens}>
            <header>
              <div class="rank-row">
                <span class="rank-badge" class:top={rank === 1}>{rank}</span>
                {#if isRecommended}
                  <span class="chip recommended">★ RECOMMENDED</span>
                {:else if degenerate.allZeroTokens}
                  <span class="chip aborted">ABORTED — NO TOKENS</span>
                {:else}
                  <span class="chip dim">PICK #{rank}</span>
                {/if}
                <span class="why-line">{why}</span>
              </div>
              <div class="addr-row">
                <b class="addr">{shortAddr(c.wallet)}</b>
                <span class="full-mono" title={c.wallet}>{c.wallet}</span>
                <button class="copy-btn" class:primary={isRecommended} onclick={() => copy(c.wallet)} title="Copy full address">{copiedAddr === c.wallet ? "✓ COPIED" : "⧉ COPY"}</button>
                <button class="copy-btn ghost" onclick={() => copy(c.wallet)} title="Copy full mint address">FULL ADDRESS</button>
                <a class="ext" href={links.solscan} target="_blank" rel="noreferrer">SOLSCAN ↗</a>
                <a class="ext" href={links.gmgn} target="_blank" rel="noreferrer">GMGN ↗</a>
              </div>
              <div class="chips">
                {#each pills as p}
                  <span class={pillClass(p.tone)}>{p.label}</span>
                {/each}
                <span class="chip dim">{c.bundledMintCount} MINT{c.bundledMintCount !== 1 ? "S" : ""}</span>
                {#if c.crossBasket}
                  <span class="chip dim">{c.crossBasket.external_creator_count} EXTERNAL CREATORS</span>
                {/if}
                {#if c.repeatInfo}
                  <span class="chip amber">{c.repeatInfo.mintCount} REPEAT · {c.repeatInfo.buy_count} BUYS</span>
                {/if}
                {#if c.dumpGroups.length > 0}
                  <span class="chip green">{c.dumpGroups.reduce((s, g) => s + g.sellCount, 0)} SELLS STAGED</span>
                {/if}
              </div>
            </header>

            <div class="summary-actions">
              <button class="details-toggle" onclick={() => toggleDetails(c.wallet)}>
                {expandedDetails.has(c.wallet) ? "▾ HIDE DETAILS" : "▸ DETAILS"}
              </button>
              <span class="details-hint">Bundle entries · repeat · cross basket · dump profile</span>
            </div>

            {#if expandedDetails.has(c.wallet)}
              <div class="sections">
                <!-- Bundle entries -->
                <div class="subsection">
                  <h3>BUNDLE ENTRIES <span>{c.bundleEntries.length} MINT{c.bundleEntries.length !== 1 ? "S" : ""}</span></h3>
                  {#if c.bundleEntries.length}
                    <table>
                      <thead><tr><th>TOKEN</th><th>ENTRY</th><th>RANK</th><th>AMOUNT</th><th>SLOT</th></tr></thead>
                      <tbody>
                        {#each c.bundleEntries as e}
                          <tr>
                            <td class="mono">{e.symbol}</td>
                            <td><span class="entry {e.entryBlock === 'B0' ? 'b0' : e.entryBlock === 'B1' ? 'b1' : 'other'}">{e.entryBlock}</span></td>
                            <td>{e.orderRank != null ? `${e.orderRank}/${e.orderTotal}` : "—"}</td>
                            <td>{formatSol(e.max_sol_cost_lamports)}</td>
                            <td>{e.creation_slot ?? "—"}</td>
                          </tr>
                        {/each}
                      </tbody>
                    </table>
                  {:else}
                    <ul><li>no bundle entries for this wallet</li></ul>
                  {/if}
                </div>

                <!-- Repeat + Cross -->
                <div class="subsection">
                  <h3>REPEAT BUNDLER <span>FINALIZED</span></h3>
                  {#if c.repeatInfo}
                    <dl>
                      <dt>MINTS</dt><dd>{c.repeatInfo.mintCount}</dd>
                      <dt>BUYS</dt><dd>{c.repeatInfo.buy_count}</dd>
                      <dt>SPAN</dt><dd>{c.repeatInfo.first_buy_slot != null ? `Slot ${c.repeatInfo.first_buy_slot}` : "—"} → {c.repeatInfo.last_buy_slot != null ? `Slot ${c.repeatInfo.last_buy_slot}` : "—"}</dd>
                    </dl>
                  {:else}
                    <ul><li>—</li></ul>
                  {/if}

                  <h3 style="margin-top:10px">CROSS-ENTITY BASKET <span>PANIER CROISÉ</span></h3>
                  {#if c.crossBasket}
                    <dl>
                      <dt>CREATORS</dt><dd>{c.crossBasket.external_creator_count}</dd>
                      <dt>BUYS</dt><dd>{c.crossBasket.buys.length}</dd>
                    </dl>
                    {#if c.crossBasket.external_creators.length}
                      <div class="basket-addrs">
                        {#each (expandedBaskets.has(c.wallet) ? c.crossBasket.external_creators : c.crossBasket.external_creators.slice(0,3)) as addr}
                          <span class="addr-pill" title={addr}>{shortAddr(addr)}</span>
                        {/each}
                        {#if c.crossBasket.external_creators.length > 3}
                          <button class="more-btn" onclick={() => toggleBasket(c.wallet)}>
                            {expandedBaskets.has(c.wallet) ? "LESS" : `+${c.crossBasket.external_creators.length - 3} MORE`}
                          </button>
                        {/if}
                      </div>
                      {#if expandedBaskets.has(c.wallet)}
                        <div class="basket-full">
                          {#each c.crossBasket.external_creators as addr}
                            <span class="mono-sm">{addr}</span>
                          {/each}
                        </div>
                      {/if}
                    {:else}
                      <ul><li>—</li></ul>
                    {/if}
                  {:else}
                    <ul><li>—</li></ul>
                  {/if}
                </div>

                <!-- Dump profile -->
                <div class="subsection">
                  <h3>DUMP PROFILE <span>FINALIZED SELLS</span></h3>
                  {#if c.dumpGroups.length}
                    <table>
                      <thead><tr><th>MINT</th><th>SELLS</th><th>SPAN</th><th>SEQUENCE</th></tr></thead>
                      <tbody>
                        {#each c.dumpGroups as g}
                          <tr>
                            <td class="mono">{shortAddr(g.mint)}</td>
                            <td>{g.sellCount}{g.buyCount ? ` · ${g.buyCount} buys` : ""}</td>
                            <td>{g.firstSellSlot != null ? `Slot ${g.firstSellSlot}` : "—"} → {g.lastSellSlot != null ? `Slot ${g.lastSellSlot}` : "—"}</td>
                            <td>
                              <span class="seq" title="{g.sellCount} sells">
                                {#each g.sells as _s, i}
                                  <i class="dot"></i>
                                {/each}
                              </span>
                              <span class="seq-count">{g.sellCount}×</span>
                            </td>
                          </tr>
                        {/each}
                      </tbody>
                    </table>
                  {:else}
                    <ul><li>no finalized sells yet</li></ul>
                  {/if}
                </div>
              </div>
            {/if}
          </section>
        {/each}
      </div>
    {/if}
  {/if}
</div>

<style>
  .copytrade-wrap { display:flex; flex-direction:column; gap:8px; }
  .empty { padding:22px; text-align:center; color:var(--on-surface-variant); font:400 12px var(--font-sans); border:1px dashed var(--outline-variant); background:var(--surface-container-low); }
  .empty b{ display:block; color:var(--on-surface); font:800 11px var(--font-mono); letter-spacing:.03em; margin-bottom:6px; }
  .empty span{ display:block; font:400 11px var(--font-sans); color:var(--on-surface-variant); max-width:640px; margin:0 auto; line-height:1.4; }
  .degenerate-empty{ border-style:solid; border-color:var(--hazard-red); background:rgba(92,20,24,.35); }
  .degenerate-empty b{ color:var(--error); }

  .clear-head{ display:flex; flex-direction:column; gap:6px; padding:8px 10px; background:var(--surface-container-lowest); border:1.5px solid var(--outline-variant); }
  .clear-title{ display:flex; align-items:center; gap:8px; color:var(--stark-white); font:800 11px var(--font-mono); letter-spacing:.06em; }
  .clear-icon{ color:var(--primary-container); font-size:13px; }
  .clear-sub{ color:var(--on-surface-variant); font:700 8px var(--font-mono); letter-spacing:.05em; }
  .clear-meta{ display:flex; justify-content:space-between; gap:10px; flex-wrap:wrap; padding:6px 10px; background:var(--surface-container-low); border:1px solid var(--outline-variant); color:var(--on-surface-variant); font:700 9px var(--font-mono); letter-spacing:.04em; }
  .hint { color:var(--on-surface-variant); font-weight:400; }

  .degenerate-banner{ display:flex; flex-direction:column; gap:8px; padding:10px 12px; background:rgba(92,20,24,.45); border:1.5px solid var(--hazard-red); color:var(--on-surface); }
  .degenerate-banner.warn{ background:rgba(92,20,24,.55); }
  .degenerate-banner .warn-icon{ color:var(--error); font-size:14px; }
  .warn-text{ display:flex; flex-direction:column; gap:2px; }
  .warn-text b{ color:var(--error); font:800 11px var(--font-mono); letter-spacing:.03em; }
  .warn-text span{ color:var(--on-surface-variant); font:400 11px var(--font-sans); line-height:1.4; }
  .warn-actions{ display:flex; align-items:center; gap:6px; flex-wrap:wrap; margin-top:2px; }
  .warn-label{ color:var(--on-surface-variant); font:700 8px var(--font-mono); letter-spacing:.05em; }
  .mono-full{ color:var(--stark-white); font:400 9px var(--font-mono); overflow-wrap:anywhere; max-width:360px; }

  .candidate-list { display:flex; flex-direction:column; gap:8px; }
  .candidate { background:var(--surface-container-lowest); border:1.5px solid var(--outline-variant); overflow:hidden; }
  .candidate.recommended{ border-color:var(--primary-container); box-shadow:0 0 0 1px rgba(255,140,0,.25); }
  .candidate.dimmed{ opacity:.92; }
  .candidate.aborted{ border-color:var(--hazard-red); opacity:.95; }
  header { display:flex; flex-direction:column; gap:6px; padding:8px 10px; background:linear-gradient(90deg, var(--surface-container-low), var(--surface-container-lowest)); border-bottom:1px solid var(--outline-variant); }
  .rank-row{ display:flex; align-items:center; gap:6px; flex-wrap:wrap; }
  .rank-badge{ display:inline-flex; align-items:center; justify-content:center; min-width:24px; height:22px; padding:0 6px; background:var(--surface-container-high); border:1px solid var(--outline-variant); color:var(--on-surface-variant); font:800 10px var(--font-mono); }
  .rank-badge.top{ background:var(--primary-container); color:#000; border-color:var(--stark-white); }
  .why-line{ flex:1; min-width:180px; color:var(--on-surface-variant); font:400 10px var(--font-mono); letter-spacing:.02em; overflow-wrap:anywhere; }
  .addr-row { display:flex; align-items:center; gap:6px; flex-wrap:wrap; }
  .addr { color:var(--tertiary); font:800 12px var(--font-mono); letter-spacing:.02em; }
  .full-mono { color:var(--on-surface-variant); font:400 9px var(--font-mono); overflow-wrap:anywhere; max-width:220px; }
  .copy-btn { padding:3px 6px; background:var(--surface-container-high); border:1px solid var(--outline-variant); color:var(--on-surface); font:700 9px var(--font-mono); cursor:pointer; }
  .copy-btn:hover { border-color:var(--primary-container); color:var(--primary-container); }
  .copy-btn.primary{ background:var(--primary-container); color:#000; border-color:var(--stark-white); }
  .copy-btn.ghost{ background:transparent; }
  .ext { padding:3px 6px; border:1px solid var(--outline-variant); background:var(--surface-container-high); color:var(--on-surface); text-decoration:none; font:700 9px var(--font-mono); }
  .ext:hover { border-color:var(--tertiary); color:var(--tertiary); }
  .chips { display:flex; gap:4px; flex-wrap:wrap; }
  .chip { padding:2px 5px; border:1px solid var(--outline-variant); background:var(--surface-container); color:var(--on-surface); font:700 8px var(--font-mono); letter-spacing:.03em; }
  .chip.dim { color:var(--on-surface-variant); }
  .chip.amber { border-color:var(--primary-container); color:var(--primary-container); background:rgba(255,140,0,.12); }
  .chip.green { border-color:var(--green-ok); color:var(--green-ok); background:rgba(16,185,129,.12); }
  .chip.leader { border-color:var(--tertiary); color:var(--tertiary); background:rgba(96,197,255,.12); }
  .chip.aborted{ border-color:var(--hazard-red); color:var(--error); background:rgba(92,20,24,.35); }
  .chip.recommended{ border-color:var(--primary-container); color:#000; background:var(--primary-container); font:800 8px var(--font-mono); }
  .summary-actions{ display:flex; align-items:center; gap:8px; padding:6px 10px; background:var(--surface-container-low); border-top:1px solid var(--outline-variant); }
  .details-toggle{ padding:3px 8px; background:var(--surface-container-high); border:1px solid var(--outline-variant); color:var(--on-surface); font:700 9px var(--font-mono); cursor:pointer; }
  .details-toggle:hover{ border-color:var(--primary-container); color:var(--primary-container); }
  .details-hint{ color:var(--on-surface-variant); font:400 9px var(--font-mono); }
  .sections { display:grid; grid-template-columns:1.2fr .9fr .9fr; gap:0; border-top:1px solid var(--outline-variant); }
  @media (max-width:1100px){ .sections{ grid-template-columns:1fr; } }
  .subsection { padding:7px 10px; border-right:1px solid var(--outline-variant); }
  .subsection:last-child{ border-right:none; }
  @media (max-width:1100px){ .subsection{ border-right:none; border-top:1px solid var(--outline-variant); } .subsection:first-child{ border-top:none; } }
  h3 { display:flex; justify-content:space-between; margin:0 0 6px; color:var(--on-surface-variant); font:800 8px var(--font-mono); letter-spacing:.04em; }
  h3 span{ color:var(--tertiary); }
  ul { max-height:200px; margin:0; padding:0; overflow-y:auto; color:var(--on-surface); font:9px var(--font-mono); list-style:none; }
  li { position:relative; margin:4px 0; padding-left:10px; overflow-wrap:anywhere; }
  li::before{ position:absolute; left:0; content:'▸'; color:var(--primary-container); }
  table { width:100%; margin:0; border-collapse:collapse; font:9px var(--font-mono); color:var(--on-surface); }
  th { padding:2px 4px; text-align:left; color:var(--on-surface-variant); font:800 8px var(--font-mono); letter-spacing:.04em; }
  td { padding:3px 4px; overflow-wrap:anywhere; border-top:1px solid var(--outline-variant); }
  .mono{ font-family:var(--font-mono); }
  .entry{ padding:1px 4px; border:1px solid var(--outline-variant); font:700 8px var(--font-mono); }
  .entry.b0{ border-color:var(--green-ok); color:var(--green-ok); background:rgba(16,185,129,.12); }
  .entry.b1{ border-color:var(--primary-container); color:var(--primary-container); background:rgba(255,140,0,.12); }
  .entry.other{ color:var(--on-surface-variant); }
  dl{ display:grid; grid-template-columns:68px 1fr; gap:3px 6px; margin:0 0 6px; font:10px var(--font-mono); }
  dt{ color:var(--on-surface-variant); font:700 8px var(--font-mono); }
  dd{ margin:0; color:var(--on-surface); overflow-wrap:anywhere; }
  .basket-addrs{ display:flex; gap:4px; flex-wrap:wrap; margin-top:4px; }
  .addr-pill{ padding:2px 5px; border:1px solid var(--outline-variant); background:var(--surface-container-low); color:var(--tertiary); font:700 9px var(--font-mono); }
  .more-btn{ padding:2px 5px; border:1px solid var(--primary-container); background:var(--surface-container-high); color:var(--primary-container); font:700 8px var(--font-mono); cursor:pointer; }
  .basket-full{ margin-top:6px; display:flex; flex-direction:column; gap:2px; max-height:90px; overflow:auto; }
  .mono-sm{ font:400 8px var(--font-mono); color:var(--on-surface-variant); overflow-wrap:anywhere; }
  .seq{ display:inline-flex; gap:3px; align-items:center; }
  .dot{ width:7px; height:7px; background:var(--secondary-container); border:1px solid var(--outline-variant); display:inline-block; }
  .seq-count{ margin-left:6px; color:var(--on-surface-variant); font:700 9px var(--font-mono); }
</style>
