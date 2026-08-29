<script>
  import { shortId, formatSol, formatPpm } from '../format.js';

  let { positions, loading } = $props();

  /** Real vs simulated must be distinguishable immediately: text + color. */
  function modeOf(position) {
    const mode = position.execution_mode;
    if (mode === 'live') return { label: 'LIVE', tone: 'acid' };
    if (mode === 'paper' || mode === 'simulation' || mode === 'simulated') {
      return { label: 'SIM', tone: 'cyan' };
    }
    return { label: '—', tone: 'muted' };
  }

  function sizeOf(position) {
    const current = position.current_position_base_units;
    const original = position.original_position_base_units;
    if (current == null || original == null || Number(original) <= 0) return '—';
    return `${Math.round((Number(current) / Number(original)) * 100)}%`;
  }

  function pnlTone(ppm) {
    return ppm == null ? '' : Number(ppm) >= 0 ? 'tone-acid' : 'tone-red';
  }
</script>

<section class="panel panel-positions" aria-label="open positions">
  <header class="panel-head">
    <h2 class="panel-title"><span class="tick tick-acid"></span>Positions</h2>
    <span class="panel-meta mono">{positions.length} open</span>
  </header>

  <div class="panel-body">
    {#if loading && !positions.length}
      <div class="skeleton-rows" aria-hidden="true">
        <div class="skeleton-line"></div>
        <div class="skeleton-line"></div>
        <div class="skeleton-line"></div>
      </div>
    {:else if !positions.length}
      <div class="empty">
        <div class="empty-title">No open positions</div>
        <div class="empty-body">Entries land here when the daemon fills a launch.</div>
      </div>
    {:else}
      <div class="position-rows">
        <div class="pos-head">
          <span>Mode</span>
          <span>Market</span>
          <span>Target</span>
          <span>Size</span>
          <span>Entry</span>
          <span>Peak</span>
          <span>TP / SL</span>
        </div>
        {#each positions as position (position.market_id)}
          {@const mode = modeOf(position)}
          <div class="position-row">
            <div class="pos-grid">
              <span class="badge badge-{mode.tone}">{mode.label}</span>
              <span class="mono pos-market" title={position.market_id}>
                {shortId(position.market_id)}
              </span>
              <span class="mono pos-target" title={position.target_id}>
                {shortId(position.target_id)}
              </span>
              <span class="num mono">{sizeOf(position)}</span>
              <span class="num mono">{formatSol(position.entry_quote_lamports)} SOL</span>
              <span class="num mono {pnlTone(position.peak_pnl_ppm)}">
                {formatPpm(position.peak_pnl_ppm)}
              </span>
              <span class="num mono pos-thresholds" title="take profit / stop loss">
                <span class="tone-acid">{formatPpm(position.take_profit_pnl_ppm)}</span>
                <span class="tone-red">{formatPpm(position.stop_loss_pnl_ppm)}</span>
              </span>
            </div>
          </div>
        {/each}
      </div>
    {/if}
  </div>
</section>