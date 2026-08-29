/**
 * Pure derivation for copytrade candidate evidence.
 * Only reads finalized fields already present on the entity report JSON.
 * No scoring, no verdicts — factual presentation only.
 */

/** @param {number|null|undefined} lamports @returns {string} */
export function formatSol(lamports) {
  if (lamports == null || !Number.isFinite(Number(lamports))) return "—";
  return `${(Number(lamports) / 1e9).toFixed(2)} SOL`;
}

/** @param {string|null|undefined} addr @returns {string} */
export function shortAddr(addr) {
  if (!addr || addr.length < 10) return addr || "—";
  return `${addr.slice(0, 4)}…${addr.slice(-4)}`;
}

/**
 * Derive ENTRY BLOCK label by SLOT offset (F6 fix).
 * B0 = same slot as creation, B1 = slot+1, B2 = slot+2, late otherwise.
 * Fail-closed: returns entry_block field when present, else slot-derived, else "—".
 * @param {number|null|undefined} buySlot
 * @param {number|null|undefined} creatorSlot
 * @returns {string}
 */
export function entryBlockLabelBySlot(buySlot, creatorSlot) {
  if (buySlot == null || creatorSlot == null) return "—";
  const delta = buySlot - creatorSlot;
  if (delta === 0) return "B0";
  if (delta === 1) return "B1";
  if (delta === 2) return "B2";
  if (delta > 2) return "late";
  return "—";
}

/** @deprecated use entryBlockLabelBySlot */
export function entryBlockLabel(buyTx, creatorTx) {
  if (buyTx == null || creatorTx == null) return "—";
  if (buyTx === creatorTx) return "B0";
  if (buyTx === creatorTx + 1) return "B1";
  return `TX ${buyTx}`;
}

/**
 * Collect unique candidate wallets from all finalized bundle sources.
 * @param {any} analysis
 * @returns {string[]}
 */
export function collectCandidateWallets(analysis) {
  if (!analysis || typeof analysis !== "object") return [];
  const set = new Set();
  const launches = analysis.launch_bundles?.launches;
  if (Array.isArray(launches)) {
    for (const l of launches) {
      if (!Array.isArray(l.buys)) continue;
      for (const b of l.buys) if (b?.wallet) set.add(b.wallet);
    }
  }
  const repeat = analysis.repeat_bundler_entities;
  if (Array.isArray(repeat)) {
    for (const r of repeat) if (r?.bundler_wallet) set.add(r.bundler_wallet);
  }
  const cross = analysis.cross_entity_bundles?.wallets;
  if (Array.isArray(cross)) {
    for (const c of cross) if (c?.wallet) set.add(c.wallet);
  }
  return [...set];
}

/**
 * @typedef {object} BundleEntry
 * @property {string} mint
 * @property {string} symbol
 * @property {number} creation_slot
 * @property {number|null} creator_transaction_index
 * @property {number|null} transaction_index
 * @property {string} entryBlock
 * @property {number|null} orderRank
 * @property {number|null} orderTotal
 * @property {number|null} max_sol_cost_lamports
 * @property {number|null} token_amount
 * @property {string} signature
 */

/**
 * Bundle entries for a specific wallet: one row per mint where wallet appears in launches[].buys.
 * Includes entry block label and order rank within bundle sorted by transaction_index.
 * @param {any} analysis
 * @param {string} wallet
 * @returns {BundleEntry[]}
 */
export function getBundleEntries(analysis, wallet) {
  if (!analysis || !wallet) return [];
  const launches = analysis.launch_bundles?.launches;
  if (!Array.isArray(launches)) return [];
  const entries = [];
  for (const launch of launches) {
    if (!Array.isArray(launch.buys)) continue;
    const buys = launch.buys;
    const sorted = [...buys].sort((a, b) => {
      if (a.transaction_index == null && b.transaction_index == null) return 0;
      if (a.transaction_index == null) return 1;
      if (b.transaction_index == null) return -1;
      return a.transaction_index - b.transaction_index;
    });
    const idxMap = new Map();
    sorted.forEach((b, i) => {
      if (b?.wallet) {
        if (!idxMap.has(b.wallet)) idxMap.set(b.wallet, []);
        idxMap.get(b.wallet).push(i + 1);
      }
    });
    for (const buy of buys) {
      if (buy.wallet !== wallet) continue;
      // rank is first occurrence position in sorted order
      const ranks = idxMap.get(wallet) || [];
      // find rank for this specific buy instance: need to match signature or index
      let rank = null;
      const pos = sorted.findIndex((s) => s.signature === buy.signature && s.wallet === buy.wallet);
      if (pos !== -1) rank = pos + 1;
      else rank = ranks[0] ?? null;
      entries.push({
        mint: launch.mint,
        symbol: launch.symbol || launch.mint.slice(0, 6),
        creation_slot: launch.creation_slot,
        creator_transaction_index: launch.creator_transaction_index ?? null,
        transaction_index: buy.transaction_index ?? null,
        entryBlock: buy.entry_block ?? entryBlockLabelBySlot(buy.slot ?? buy.creation_slot ?? null, launch.creation_slot),
        orderRank: rank,
        orderTotal: buys.length,
        max_sol_cost_lamports: buy.max_sol_cost_lamports ?? null,
        token_amount: buy.token_amount ?? null,
        signature: buy.signature || "",
        buy_slot: buy.slot ?? null,
        creation_slot: launch.creation_slot,
      });
    }
  }
  // sort entries by creation_slot ascending for stable display
  entries.sort((a, b) => (a.creation_slot ?? 0) - (b.creation_slot ?? 0));
  return entries;
}

/**
 * Repeat bundler line for wallet.
 * @param {any} analysis
 * @param {string} wallet
 * @returns {{ bundler_wallet:string, mintCount:number, buy_count:number, first_buy_slot:number, last_buy_slot:number, mints:string[] }|null}
 */
export function getRepeatBundlerInfo(analysis, wallet) {
  const list = analysis?.repeat_bundler_entities;
  if (!Array.isArray(list)) return null;
  const found = list.find((r) => r.bundler_wallet === wallet);
  if (!found) return null;
  return {
    bundler_wallet: found.bundler_wallet,
    mintCount: Array.isArray(found.mints) ? found.mints.length : 0,
    buy_count: found.buy_count ?? 0,
    first_buy_slot: found.first_buy_slot ?? null,
    last_buy_slot: found.last_buy_slot ?? null,
    mints: Array.isArray(found.mints) ? found.mints : [],
  };
}

/**
 * Cross-entity basket for wallet.
 * @param {any} analysis
 * @param {string} wallet
 * @returns {{ wallet:string, external_creator_count:number, external_creators:string[], buys:any[] }|null}
 */
export function getCrossEntityBasket(analysis, wallet) {
  const wallets = analysis?.cross_entity_bundles?.wallets;
  if (!Array.isArray(wallets)) return null;
  const found = wallets.find((w) => w.wallet === wallet);
  if (!found) return null;
  return {
    wallet: found.wallet,
    external_creator_count: found.external_creator_count ?? 0,
    external_creators: Array.isArray(found.external_creators) ? found.external_creators : [],
    buys: Array.isArray(found.buys) ? found.buys : [],
  };
}

/**
 * @typedef {object} DumpMintGroup
 * @property {string} mint
 * @property {number} sellCount
 * @property {number|null} buyCount
 * @property {number|null} firstSellSlot
 * @property {number|null} lastSellSlot
 * @property {{slot:number, transaction_index:number|null, signature:string}[]} sells
 * @property {{slot:number, transaction_index:number|null}[]} buys
 */

/**
 * Derive staged dump profile by grouping pump_trades sells per mint for wallet.
 * Sorted by (slot, transaction_index).
 * @param {any} analysis
 * @param {string} wallet
 * @returns {DumpMintGroup[]}
 */
export function getDumpProfile(analysis, wallet) {
  const trades = analysis?.pump_trades;
  if (!Array.isArray(trades) || !wallet) return [];
  const relevant = trades.filter((t) => t.wallet === wallet);
  if (relevant.length === 0) return [];
  const byMint = new Map();
  for (const t of relevant) {
    if (!t.mint) continue;
    if (!byMint.has(t.mint)) byMint.set(t.mint, []);
    byMint.get(t.mint).push(t);
  }
  const groups = [];
  for (const [mint, arr] of byMint.entries()) {
    const sells = arr
      .filter((x) => x.side === "sell")
      .sort((a, b) => (a.slot ?? 0) - (b.slot ?? 0) || (a.transaction_index ?? 0) - (b.transaction_index ?? 0));
    const buys = arr
      .filter((x) => x.side === "buy")
      .sort((a, b) => (a.slot ?? 0) - (b.slot ?? 0) || (a.transaction_index ?? 0) - (b.transaction_index ?? 0));
    if (sells.length === 0) continue;
    groups.push({
      mint,
      sellCount: sells.length,
      buyCount: buys.length,
      firstSellSlot: sells[0]?.slot ?? null,
      lastSellSlot: sells[sells.length - 1]?.slot ?? null,
      sells: sells.map((s) => ({ slot: s.slot, transaction_index: s.transaction_index ?? null, signature: s.signature || "" })),
      buys: buys.map((b) => ({ slot: b.slot, transaction_index: b.transaction_index ?? null })),
    });
  }
  groups.sort((a, b) => b.sellCount - a.sellCount || (a.firstSellSlot ?? 0) - (b.firstSellSlot ?? 0));
  return groups;
}

/**
 * Full evidence for one candidate wallet.
 * @param {any} analysis
 * @param {string} wallet
 * @returns {{ wallet:string, bundleEntries:BundleEntry[], repeatInfo:any, crossBasket:any, dumpGroups:DumpMintGroup[] }}
 */
export function getCandidateEvidence(analysis, wallet) {
  return {
    wallet,
    bundleEntries: getBundleEntries(analysis, wallet),
    repeatInfo: getRepeatBundlerInfo(analysis, wallet),
    crossBasket: getCrossEntityBasket(analysis, wallet),
    dumpGroups: getDumpProfile(analysis, wallet),
  };
}

/**
 * Sorted candidate list by evidence density: bundled mints desc, then cross-entity count desc.
 * @param {any} analysis
 * @returns {{ wallet:string, bundledMintCount:number, crossCreatorCount:number, bundleEntries:BundleEntry[], repeatInfo:any, crossBasket:any, dumpGroups:DumpMintGroup[] }[]}
 */
export function getSortedCandidates(analysis) {
  const wallets = collectCandidateWallets(analysis);
  const candidates = wallets.map((w) => {
    const ev = getCandidateEvidence(analysis, w);
    return {
      wallet: w,
      bundledMintCount: ev.bundleEntries.length,
      crossCreatorCount: ev.crossBasket?.external_creator_count ?? 0,
      ...ev,
    };
  });
  candidates.sort((a, b) => {
    if (b.bundledMintCount !== a.bundledMintCount) return b.bundledMintCount - a.bundledMintCount;
    if (b.crossCreatorCount !== a.crossCreatorCount) return b.crossCreatorCount - a.crossCreatorCount;
    // tie-breaker: more sells evidence first
    const aSells = a.dumpGroups.reduce((s, g) => s + g.sellCount, 0);
    const bSells = b.dumpGroups.reduce((s, g) => s + g.sellCount, 0);
    if (bSells !== aSells) return bSells - aSells;
    return a.wallet.localeCompare(b.wallet);
  });
  return candidates;
}

/**
 * Factual pill derivation — no scores, only observed facts.
 * Tones map to existing chip colors: leader(tertiary), repeat(amber/primary), dump(green), burner(dim), aborted(error)
 * @param {{ bundleEntries:BundleEntry[], repeatInfo:any, crossBasket:any, dumpGroups:DumpMintGroup[] }} candidate
 * @returns {{ label:string, tone:string }[]}
 */
export function deriveFactualPills(candidate) {
  const pills = [];
  const hasLeader = candidate.bundleEntries.some((e) => e.orderRank === 1 || e.entryBlock === "B0");
  if (hasLeader) pills.push({ label: "LEADER", tone: "leader" });
  if (candidate.repeatInfo) pills.push({ label: "REPEAT", tone: "repeat" });
  const totalSells = candidate.dumpGroups.reduce((s, g) => s + g.sellCount, 0);
  const staged = candidate.dumpGroups.some((g) => g.sellCount >= 2) || totalSells >= 2;
  if (staged) pills.push({ label: "STAGED DUMP", tone: "dump" });
  const isBurner = candidate.bundleEntries.length > 0 && candidate.bundleEntries.some((e) => e.token_amount === 0);
  const allZero = candidate.bundleEntries.length > 0 && candidate.bundleEntries.every((e) => e.token_amount === 0 || e.token_amount == null);
  if (isBurner) pills.push({ label: "BURNER", tone: "burner" });
  if (allZero && candidate.bundleEntries.length > 0) pills.push({ label: "ABORTED", tone: "aborted" });
  if (candidate.bundleEntries.length === 0 && (candidate.repeatInfo || candidate.crossBasket)) {
    pills.push({ label: "NO BUNDLE", tone: "aborted" });
  }
  return pills;
}

/**
 * One-line factual why for glanceable ranking.
 * @param {{ wallet:string, bundleEntries:BundleEntry[], repeatInfo:any, crossBasket:any, dumpGroups:DumpMintGroup[] }} candidate
 * @returns {string}
 */
export function deriveWhyLine(candidate) {
  const totalSells = candidate.dumpGroups.reduce((s, g) => s + g.sellCount, 0);
  const b0 = candidate.bundleEntries.filter((e) => e.entryBlock === "B0").length;
  const b1 = candidate.bundleEntries.filter((e) => e.entryBlock === "B1").length;
  const allZero = candidate.bundleEntries.length > 0 && candidate.bundleEntries.every((e) => e.token_amount === 0 || e.token_amount == null);
  if (allZero) return "0 tokens · funded then swept — no executable buy";
  let entryLabel = "—";
  if (b0 > 0) entryLabel = `B0 ×${b0}`;
  else if (b1 > 0) entryLabel = `B1 ×${b1}`;
  else if (candidate.bundleEntries[0]?.entryBlock) entryLabel = candidate.bundleEntries[0].entryBlock;
  const bundlePart = `${candidate.bundleEntries.length} bundle mint${candidate.bundleEntries.length !== 1 ? "s" : ""}`;
  const repeatPart = candidate.repeatInfo ? `${candidate.repeatInfo.mintCount} repeat / ${candidate.repeatInfo.buy_count} buys` : "no repeat";
  const crossPart = `${candidate.crossBasket?.external_creator_count ?? 0} external creators`;
  const dumpPart = totalSells > 0 ? `${totalSells} sells${candidate.dumpGroups.some((g) => g.sellCount >= 2) ? " staged" : ""}` : "no sells";
  const leaderPart = candidate.bundleEntries.some((e) => e.orderRank === 1) ? "rank 1" : entryLabel;
  return `${leaderPart} · ${bundlePart} · ${repeatPart} · ${crossPart} · ${dumpPart}`;
}

/**
 * Detect degenerate / aborted bundle state (e.g. B7GtpB8...: 8 funders swept 0 tokens, only creator sell).
 * Factual only — counts derived from finalized report.
 * @param {any} report
 * @param {any[]} candidates
 * @returns {{ isDegenerate:boolean, funderCount:number, allZeroTokens:boolean, creatorWallet:string|null, hasCreatorSell:boolean, bundledLaunchCount:number, message:string|null }}
 */
export function detectDegenerateState(report, candidates) {
  const launches = report?.launch_bundles?.launches;
  const bundledLaunchCount = report?.launch_bundles?.bundled_launch_count ?? (Array.isArray(launches) ? launches.length : 0);
  const totalBuys = Array.isArray(launches) ? launches.reduce((s, l) => s + (Array.isArray(l.buys) ? l.buys.length : 0), 0) : 0;
  const totalEntries = Array.isArray(candidates) ? candidates.reduce((s, c) => s + (c.bundleEntries?.length ?? 0), 0) : 0;
  const allZeroTokens =
    Array.isArray(candidates) &&
    candidates.length > 0 &&
    candidates.every((c) => c.bundleEntries.length === 0 || c.bundleEntries.every((e) => e.token_amount === 0 || e.token_amount == null));
  const isDegenerate = !candidates || candidates.length === 0 || bundledLaunchCount === 0 || totalBuys === 0 || totalEntries === 0 || allZeroTokens;
  const creatorWallet = report?.tracking_address || report?.target_wallet || report?.identity?.input || null;
  const trades = report?.pump_trades;
  const hasCreatorSell = Array.isArray(trades) && creatorWallet ? trades.some((t) => t.wallet === creatorWallet && t.side === "sell") : false;
  const funderCount = Array.isArray(candidates) ? candidates.length : 0;
  let message = null;
  if (isDegenerate) {
    if (allZeroTokens && funderCount > 0) {
      message = `NO BUNDLE BUYS — ${funderCount} funder${funderCount !== 1 ? "s" : ""} swept 0 tokens${hasCreatorSell ? ", only creator sell" : ""}`;
    } else if (totalBuys === 0 && hasCreatorSell) {
      const fc = funderCount > 0 ? `${funderCount} funders swept 0 tokens, ` : "";
      message = `NO BUNDLE BUYS — ${fc}only creator sell`;
    } else if (candidates.length === 0) {
      message = hasCreatorSell ? "NO BUNDLE BUYS — only creator sell" : "NO BUNDLE BUYS — no executable buy wallets";
    }
  }
  return { isDegenerate, funderCount, allZeroTokens, creatorWallet, hasCreatorSell, bundledLaunchCount, message };
}
