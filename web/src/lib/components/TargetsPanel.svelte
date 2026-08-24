<script>
  import { shortId } from '../format.js';

  let { targets, loading } = $props();

  /**
   * Per-target execution state, mirroring the TUI product contract:
   * LIVE (monitoring + real submission), SIMULATED (monitoring + paper),
   * PAUSED (monitoring disabled), TRACKED (no policy -> not eligible),
   * OFF (policy present, execution off).
   */
  function modeOf(target) {
    const policy = target && typeof target.policy === 'object' ? target.policy : null;
    if (policy && policy.monitoring_enabled === false) return { label: 'PAUSED', tone: 'amber' };
    if (policy && policy.execution_mode === 'live') return { label: 'LIVE', tone: 'acid' };
    if (policy && policy.execution_mode === 'simulated') return { label: 'SIMULATED', tone: 'cyan' };
    if (!policy) return { label: 'TRACKED', tone: 'muted' };
    return { label: 'OFF', tone: 'muted' };
  }
</script>

<section class="panel panel-targets" aria-label="tracked targets">
  <header class="panel-head">
    <h2 class="panel-title"><span class="tick tick-cyan"></span>Targets</h2>
    <span class="panel-meta mono">{targets.length} tracked</span>
  </header>

  <div class="panel-body">
    {#if loading && !targets.length}
      <div class="skeleton-rows" aria-hidden="true">
        <div class="skeleton-line"></div>
        <div class="skeleton-line"></div>
        <div class="skeleton-line"></div>
        <div class="skeleton-line"></div>
        <div class="skeleton-line"></div>
      </div>
    {:else if !targets.length}
      <div class="empty">
        <div class="empty-title">No targets watched</div>
        <div class="empty-body">
          Register a dev wallet with <code>watch</code> in the command rail.
        </div>
      </div>
    {:else}
      <table class="data-table">
        <thead>
          <tr>
            <th>Mode</th>
            <th>Address</th>
            <th>Label</th>
            <th class="num">Lch</th>
            <th class="num">WR</th>
          </tr>
        </thead>
        <tbody>
          {#each targets as target (target.address)}
            {@const mode = modeOf(target)}
            <tr>
              <td>
                <span class="badge badge-{mode.tone}">{mode.label}</span>
              </td>
              <td class="mono" title={target.address}>{shortId(target.address)}</td>
              <td class="label-cell" title={target.label ?? ''}>{target.label ?? '—'}</td>
              <td class="num mono">{target.launches_count ?? '—'}</td>
              <td class="num mono">
                {target.winrate_pct != null ? `${Number(target.winrate_pct).toFixed(1)}%` : '—'}
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    {/if}
  </div>
</section>