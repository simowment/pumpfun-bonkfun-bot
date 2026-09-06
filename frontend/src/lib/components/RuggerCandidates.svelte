<script>
  import { accountLinks } from '../externalLinks.js';

  let { ruggers = [], loading = false, error = '', type2Note = '', onScan = null } = $props();

  let copiedAddr = $state(null);
  let expanded = $state(new Set());

  function shortAddr(a) {
    if (!a || a.length < 10) return a || '—';
    return `${a.slice(0, 4)}...${a.slice(-4)}`;
  }

  function copy(addr) {
    if (!addr) return;
    navigator.clipboard.writeText(addr);
    copiedAddr = addr;
    setTimeout(() => (copiedAddr = null), 1800);
  }

  // Svelte 5 removed event modifiers; stop row-toggle propagation manually.
  function copyStop(e, addr) {
    e.stopPropagation();
    copy(addr);
  }

  function stop(e) {
    e.stopPropagation();
  }

  function scanStop(e, addr) {
    e.stopPropagation();
    if (onScan) onScan(addr);
  }

  function toggle(addr) {
    const next = new Set(expanded);
    if (next.has(addr)) next.delete(addr);
    else next.add(addr);
    expanded = next;
  }

  // ppm multipliers -> "1.49x"; 1_000_000 ppm == 1.0x
  function mult(ppm) {
    if (ppm == null) return '—';
    return `${(ppm / 1000000).toFixed(2)}x`;
  }
  // ppm rate -> percent; 10_000 ppm == 1%
  function pct(ppm) {
    if (ppm == null) return '—';
    return `${(ppm / 10000).toFixed(0)}%`;
  }
  function cadence(minutes) {
    if (minutes == null) return '—';
    if (minutes < 1) return `${(minutes * 60).toFixed(0)}s`;
    if (minutes < 60) return `${minutes.toFixed(1)}m`;
    return `${(minutes / 60).toFixed(1)}h`;
  }
  function ts(iso) {
    if (!iso) return '—';
    return iso.replace('T', ' ').slice(0, 19) + ' UTC';
  }
  function shortFunding(s) {
    if (!s) return '—';
    return s.length > 22 ? `${s.slice(0, 22)}…` : s;
  }

  const STATUS_TONE = {
    stats_manual: 'green',
    abstain: 'dim',
    no_rpc_in_window_only: 'amber',
    mass_spammer: 'red',
  };
  function statusTone(status) {
    return STATUS_TONE[status] ?? 'dim';
  }
  function scoreCommand(addr) {
    return `uv run rug_check ${addr} --score --entity`;
  }
</script>

<div class="rugger-wrap">
  <div class="clear-head">
    <div class="clear-title">
      <span class="clear-icon">☠</span>
      RUGGER ENTITIES
      <span class="clear-sub">BIBLE §1 SPAM CAP · FUNDING CLUSTERS · STATS MANUAL</span>
    </div>
    <div class="clear-meta">
      {#if loading}
        <span>SCANNING DISCOVER DB + LIVE RPC…</span>
      {:else if ruggers.length}
        <span>{ruggers.length} ENTIT{ruggers.length !== 1 ? 'IES' : 'Y'} · SORTED BY IN-WINDOW LAUNCHES</span>
        <span class="hint">Winrate/EV are manual via rug_check · tap a row for the funding cluster</span>
      {:else}
        <span>NO RUGGER ENTITIES RANKED</span>
        <span class="hint">Run the headless collector, or widen the window / lower min-launches</span>
      {/if}
    </div>
  </div>

  {#if error}
    <div class="empty error"><b>Unable to rank ruggers</b><span>{error}</span></div>
  {:else if loading && !ruggers.length}
    <div class="empty">Loading ranked rugger entities…</div>
  {:else if !ruggers.length}
    <div class="empty">
      <b>No ranked entities yet</b>
      <span>The discover database has no creator wallet with enough launches in this window.</span>
    </div>
  {:else}
    <div class="table-container">
      <table class="rugger-table">
        <thead>
          <tr>
            <th>#</th>
            <th>ENTITY (OPERATOR)</th>
            <th>LIFETIME</th>
            <th>IN-WIN</th>
            <th>FUNDING</th>
            <th>ARCHETYPE</th>
            <th>STATUS</th>
            <th>ACTION</th>
          </tr>
        </thead>
        <tbody>
          {#each ruggers as r (r.operator)}
            {@const links = accountLinks(r.operator)}
            {@const profile = r.ath_exit_profile ?? {}}
            {@const cad = r.launch_cadence ?? {}}
            {@const qual = r.qualification ?? {}}
            {@const cluster = (r.entity_wallets ?? []).length}
            <tr class="row" class:top={r.rank === 1} onclick={() => toggle(r.operator)}>
              <td class="rank" class:toprank={r.rank === 1}>{r.rank}</td>
              <td>
                <div class="wallet-cell">
                  <span class="mono-addr bold">{shortAddr(r.operator)}</span>
                  {#if cluster > 1}<span class="cluster-pill" title="funding-cluster wallets">×{cluster}</span>{/if}
                  <button class="copy-btn" onclick={(e) => copyStop(e, r.operator)}>{copiedAddr === r.operator ? '✓' : '⧉'}</button>
                </div>
              </td>
              <td class="mono lifetime" title="lifetime pump.fun creations (spam cap gate)">{r.lifetime_creation_count ?? '?'}</td>
              <td class="mono text-muted" title="launches observed in the collect window">{r.in_window_launch_count}</td>
              <td class="mono funding" title={r.funding_summary ?? ''}>{r.funding_summary ? shortFunding(r.funding_summary) : '—'}</td>
              <td>
                {#if r.archetype === 'type2_funding_cluster'}
                  <span class="chip type2" title="wallet-switcher: fresh burners funded by a recurrent source">TYPE 2</span>
                {:else if r.archetype === 'mass_spammer'}
                  <span class="chip red">SPAMMER</span>
                {:else}
                  <span class="chip amber">TYPE 1</span>
                {/if}
              </td>
              <td><span class="chip {statusTone(qual.status)}" title={qual.message}>{qual.status ?? '—'}</span></td>
              <td>
                <div class="actions">
                  <button class="pill-btn active" onclick={(e) => scanStop(e, r.operator)} title="Load & scan this operator wallet">SCAN</button>
                  <a class="ext" href={links.solscan} target="_blank" rel="noreferrer" onclick={stop}>SOLSCAN ↗</a>
                  <a class="ext" href={links.gmgn} target="_blank" rel="noreferrer" onclick={stop}>GMGN ↗</a>
                </div>
              </td>
            </tr>
            {#if expanded.has(r.operator)}
              <tr class="detail-row">
                <td colspan="8">
                  <div class="detail-grid">
                    <div class="detail-block">
                      <h4>FUNDING CLUSTER (ENTITY)</h4>
                      <dl>
                        <dt>ARCHETYPE</dt><dd>{r.archetype}</dd>
                        <dt>FUNDING</dt><dd>{r.funding_summary ?? '—'}</dd>
                        <dt>TOP FUNDER</dt>
                        <dd>
                          {#if r.primary_funder}
                            <span class="mono-sm">{r.primary_funder}</span>
                            <button class="copy-btn" onclick={(e) => copyStop(e, r.primary_funder)}>⧉</button>
                          {:else}—{/if}
                        </dd>
                        <dt>WALLETS</dt><dd>{cluster}</dd>
                      </dl>
                      <div class="entity-wallets">
                        {#each (r.entity_wallets ?? []) as w}
                          <span class="ew mono-sm">{w}<button class="copy-btn" onclick={(e) => copyStop(e, w)}>⧉</button></span>
                        {/each}
                      </div>
                      <p class="archetype-note">{r.archetype_note ?? ''}</p>
                    </div>
                    <div class="detail-block">
                      <h4>MANUAL STATS (BIBLE §1 STEP 5)</h4>
                      <dl>
                        <dt>STATUS</dt><dd>{qual.status ?? '—'}</dd>
                      </dl>
                      <p class="qual-msg">Winrate/EV are not auto-computed. Score this entity yourself:</p>
                      <div class="score-cmd">
                        <span class="mono-sm">{scoreCommand(r.operator)}</span>
                        <button class="copy-btn" onclick={(e) => copyStop(e, scoreCommand(r.operator))}>{copiedAddr === scoreCommand(r.operator) ? '✓' : '⧉'}</button>
                      </div>
                      <p class="next-action"><b>NEXT:</b> {r.next_action ?? ''}</p>
                    </div>
                    <div class="detail-block">
                      <h4>IN-WINDOW OBSERVED (COLLECT DB)</h4>
                      <dl>
                        <dt>LAUNCHES</dt><dd>{cad.launch_count ?? r.in_window_launch_count}</dd>
                        <dt>FIRST</dt><dd>{ts(cad.first_created_at)}</dd>
                        <dt>LAST</dt><dd>{ts(cad.last_created_at)}</dd>
                        <dt>MEDIAN</dt><dd>{cadence(cad.median_interval_minutes)}</dd>
                        <dt>MED ATH</dt><dd>{mult(profile.median_ath_multiplier_ppm)}</dd>
                        <dt>PEAK ATH</dt><dd>{mult(profile.peak_ath_multiplier_ppm)}</dd>
                        <dt>DEV-DUMP</dt><dd>{profile.dev_dump_count ?? 0}/{r.in_window_launch_count} ({pct(profile.dev_dump_rate_ppm)})</dd>
                      </dl>
                      <div class="full-mono">{r.operator}</div>
                    </div>
                  </div>
                </td>
              </tr>
            {/if}
          {/each}
        </tbody>
      </table>
    </div>
  {/if}

  {#if type2Note}
    <div class="type2-note"><b>ENTITY MODEL:</b> {type2Note}</div>
  {/if}
</div>

<style>
  .rugger-wrap { display: flex; flex-direction: column; gap: 8px; }
  .clear-head { display: flex; flex-direction: column; gap: 6px; padding: 8px 10px; background: var(--surface-container-lowest); border: 1.5px solid var(--outline-variant); }
  .clear-title { display: flex; align-items: center; gap: 8px; color: var(--stark-white); font: 800 11px var(--font-mono); letter-spacing: .06em; }
  .clear-icon { color: var(--error); font-size: 13px; }
  .clear-sub { color: var(--on-surface-variant); font: 700 8px var(--font-mono); letter-spacing: .05em; }
  .clear-meta { display: flex; justify-content: space-between; gap: 10px; flex-wrap: wrap; padding: 6px 10px; background: var(--surface-container-low); border: 1px solid var(--outline-variant); color: var(--on-surface-variant); font: 700 9px var(--font-mono); letter-spacing: .04em; }
  .hint { color: var(--on-surface-variant); font-weight: 400; }

  .empty { padding: 22px; text-align: center; color: var(--on-surface-variant); font: 400 12px var(--font-sans); border: 1px dashed var(--outline-variant); background: var(--surface-container-low); }
  .empty b { display: block; color: var(--on-surface); font: 800 11px var(--font-mono); letter-spacing: .03em; margin-bottom: 6px; }
  .empty span { display: block; font: 400 11px var(--font-sans); max-width: 640px; margin: 0 auto; line-height: 1.4; }
  .empty.error { border-style: solid; border-color: var(--hazard-red); background: rgba(92, 20, 24, .35); }
  .empty.error b { color: var(--error); }

  .table-container { overflow-x: auto; max-height: 340px; border: 1px solid var(--outline-variant); }
  .rugger-table { width: 100%; border-collapse: collapse; font-size: 12px; font-family: var(--font-sans); }
  .rugger-table th { position: sticky; top: 0; text-align: left; padding: 7px 8px; color: var(--on-surface-variant); font: 800 9px var(--font-mono); letter-spacing: .04em; background: var(--surface-container-low); border-bottom: 1px solid var(--outline-variant); }
  .rugger-table td { padding: 6px 8px; border-bottom: 1px solid var(--outline-variant); color: var(--on-surface); }
  .row { cursor: pointer; }
  .row:hover td { background: var(--surface-container-high); }
  .row.top td { background: rgba(255, 140, 0, .06); }
  .rank { font: 800 11px var(--font-mono); color: var(--on-surface-variant); }
  .rank.toprank { color: var(--primary-container); }
  .wallet-cell { display: flex; align-items: center; gap: 5px; }
  .mono-addr { font-family: var(--font-mono); font-size: 12px; color: var(--tertiary); }
  .bold { font-weight: 700; }
  .cluster-pill { padding: 1px 4px; border: 1px solid var(--tertiary); color: var(--tertiary); font: 700 8px var(--font-mono); }
  .lifetime { color: var(--primary-container); font-size: 12px; }
  .funding { color: var(--on-surface-variant); font-size: 10px; max-width: 160px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .mono { font-family: var(--font-mono); }
  .text-muted { color: var(--on-surface-variant); }
  .copy-btn { padding: 1px 5px; background: var(--surface-container-high); border: 1px solid var(--outline-variant); color: var(--on-surface); font: 700 9px var(--font-mono); cursor: pointer; }
  .copy-btn:hover { border-color: var(--primary-container); color: var(--primary-container); }
  .chip { padding: 2px 5px; border: 1px solid var(--outline-variant); background: var(--surface-container); color: var(--on-surface); font: 700 8px var(--font-mono); letter-spacing: .03em; }
  .chip.dim { color: var(--on-surface-variant); }
  .chip.amber { border-color: var(--primary-container); color: var(--primary-container); background: rgba(255, 140, 0, .12); }
  .chip.green { border-color: var(--green-ok); color: var(--green-ok); background: rgba(0, 200, 83, .1); }
  .chip.red { border-color: var(--hazard-red); color: var(--error); background: rgba(92, 20, 24, .35); }
  .chip.type2 { border-color: var(--tertiary); color: var(--tertiary); background: rgba(124, 77, 255, .12); }
  .actions { display: flex; align-items: center; gap: 4px; flex-wrap: wrap; }
  .pill-btn { background: var(--surface-container-high); border: 1px solid var(--outline-variant); color: var(--on-surface-variant); font: 600 10px var(--font-sans); padding: 3px 8px; cursor: pointer; border-radius: 3px; }
  .pill-btn.active { background: var(--primary-container); color: #000; border-color: var(--stark-white); font-weight: 700; }
  .ext { padding: 3px 6px; border: 1px solid var(--outline-variant); background: var(--surface-container-high); color: var(--on-surface); text-decoration: none; font: 700 9px var(--font-mono); }
  .ext:hover { border-color: var(--tertiary); color: var(--tertiary); }

  .detail-row td { background: var(--surface-container-lowest); padding: 10px; }
  .detail-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }
  @media (max-width: 1100px) { .detail-grid { grid-template-columns: 1fr; } }
  .detail-block { border: 1px solid var(--outline-variant); background: var(--surface-container-low); padding: 8px 10px; }
  .detail-block h4 { margin: 0 0 6px; color: var(--tertiary); font: 800 9px var(--font-mono); letter-spacing: .04em; }
  dl { display: grid; grid-template-columns: 110px 1fr; gap: 3px 6px; margin: 0; font: 10px var(--font-mono); }
  dt { color: var(--on-surface-variant); font: 700 8px var(--font-mono); }
  dd { margin: 0; color: var(--on-surface); overflow-wrap: anywhere; }
  .mono-sm { font: 400 9px var(--font-mono); }
  .entity-wallets { display: flex; flex-direction: column; gap: 3px; margin-top: 6px; }
  .ew { display: flex; align-items: center; gap: 4px; color: var(--on-surface-variant); overflow-wrap: anywhere; }
  .score-cmd { display: flex; align-items: center; gap: 6px; margin-top: 6px; padding: 4px 6px; border: 1px solid var(--outline-variant); background: var(--surface-container); color: var(--green-ok); overflow-wrap: anywhere; }
  .qual-msg { margin: 6px 0 0; color: var(--on-surface-variant); font: 400 10px var(--font-sans); line-height: 1.4; }
  .archetype-note { margin: 6px 0 0; color: var(--on-surface-variant); font: 400 10px var(--font-sans); line-height: 1.4; }
  .next-action { margin: 6px 0 0; color: var(--primary-container); font: 700 10px var(--font-mono); line-height: 1.4; }
  .full-mono { margin-top: 8px; color: var(--on-surface-variant); font: 400 9px var(--font-mono); overflow-wrap: anywhere; }
  .type2-note { padding: 8px 10px; border: 1px dashed var(--outline-variant); background: var(--surface-container-low); color: var(--on-surface-variant); font: 400 10px var(--font-sans); line-height: 1.4; }
  .type2-note b { color: var(--tertiary); font: 800 9px var(--font-mono); }
</style>
