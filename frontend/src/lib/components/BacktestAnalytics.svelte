<script>
  import { onMount } from 'svelte';
  import { backtestEntity } from '../api.js';

  let { targetAddress = '' } = $props();

  let quoteSize = $state(0.10);
  let slippagePct = $state(1.5);
  let loading = $state(false);
  let result = $state(null);
  let error = $state(null);

  async function runBacktest() {
    if (!targetAddress) return;
    loading = true;
    error = null;
    try {
      result = await backtestEntity(targetAddress, quoteSize, slippagePct);
    } catch (e) {
      error = e.message;
    } finally {
      loading = false;
    }
  }

  onMount(() => {
    if (targetAddress) {
      runBacktest();
    }
  });
</script>

<div class="backtest-container">
  <div class="backtest-controls">
    <div class="field">
      <label for="quote-input">Quote Size (SOL):</label>
      <input id="quote-input" type="number" bind:value={quoteSize} step="0.05" min="0.01" max="10" />
    </div>
    <div class="field">
      <label for="slip-input">Slippage (%):</label>
      <input id="slip-input" type="number" bind:value={slippagePct} step="0.5" min="0.1" max="10" />
    </div>
    <button class="btn btn-hazard" onclick={runBacktest} disabled={loading}>
      {loading ? 'Simulating...' : 'Run Simulation'}
    </button>
  </div>

  {#if error}
    <div class="error-msg">{error}</div>
  {/if}

  {#if result}
    <div class="results-grid">
      <div class="metric-card">
        <span class="m-label">Net PnL</span>
        <span class="m-val {result.net_pnl_sol >= 0 ? 'green-text' : 'red-text'}">
          {result.net_pnl_sol >= 0 ? '+' : ''}{result.net_pnl_sol.toFixed(4)} SOL
        </span>
      </div>
      <div class="metric-card">
        <span class="m-label">Net ROI</span>
        <span class="m-val {result.net_roi_pct >= 0 ? 'green-text' : 'red-text'}">
          {result.net_roi_pct >= 0 ? '+' : ''}{result.net_roi_pct.toFixed(1)}%
        </span>
      </div>
      <div class="metric-card">
        <span class="m-label">Win Rate</span>
        <span class="m-val stark-white">{result.win_rate_pct.toFixed(0)}%</span>
      </div>
      <div class="metric-card">
        <span class="m-label">Optimal Take Profit</span>
        <span class="m-val hazard-orange">{result.optimal_tp_label || '+100%'}</span>
      </div>
    </div>

    {#if result.launches && result.launches.length > 0}
      <table class="sim-table">
        <thead>
          <tr>
            <th>Token</th>
            <th>Peak Multiplier</th>
            <th>PnL (SOL)</th>
            <th>Exit Reason</th>
          </tr>
        </thead>
        <tbody>
          {#each result.launches as l}
            <tr>
              <td><strong>{l.symbol}</strong></td>
              <td class="mono">{l.peak_mult.toFixed(2)}x</td>
              <td class="mono {l.pnl_sol >= 0 ? 'green-text' : 'red-text'}">
                {l.pnl_sol >= 0 ? '+' : ''}{l.pnl_sol.toFixed(4)} SOL
              </td>
              <td>{l.exit_reason || 'TP Hit'}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    {/if}
  {/if}
</div>

<style>
  .backtest-container {
    display: flex;
    flex-direction: column;
    gap: 16px;
    font-family: var(--font-sans);
  }
  .backtest-controls {
    display: flex;
    align-items: center;
    gap: 16px;
    background-color: var(--surface-container-low);
    padding: 12px 16px;
    border: 1px solid var(--outline-variant);
    flex-wrap: wrap;
  }
  .field {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 13px;
  }
  .field label {
    font-weight: 600;
    color: var(--on-surface-variant);
  }
  .field input {
    background-color: var(--surface-container-high);
    border: 1px solid var(--outline-variant);
    color: var(--on-surface);
    padding: 6px 10px;
    font-family: var(--font-mono);
    font-size: 13px;
    width: 90px;
  }
  .field input:focus {
    border-color: var(--primary-container);
  }
  .results-grid {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 12px;
  }
  .metric-card {
    background-color: var(--surface-container-low);
    border: 1px solid var(--outline-variant);
    padding: 12px 14px;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .m-label {
    font-size: 11.5px;
    color: var(--on-surface-variant);
    font-weight: 600;
    text-transform: uppercase;
  }
  .m-val {
    font-family: var(--font-mono);
    font-size: 18px;
    font-weight: 700;
  }
  .stark-white {
    color: var(--stark-white);
  }
  .hazard-orange {
    color: var(--primary-container);
  }
  .green-text {
    color: var(--green-ok);
  }
  .red-text {
    color: var(--error);
  }
  .mono {
    font-family: var(--font-mono);
  }
  .sim-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
    margin-top: 8px;
  }
  .sim-table th {
    text-align: left;
    padding: 8px 12px;
    background-color: var(--surface-container-low);
    border-bottom: 1px solid var(--outline-variant);
    color: var(--on-surface-variant);
    font-size: 11.5px;
  }
  .sim-table td {
    padding: 8px 12px;
    border-bottom: 1px solid var(--outline-variant);
  }
  .error-msg {
    color: var(--error);
    background-color: var(--error-container);
    padding: 10px;
    border: 1px solid var(--hazard-red);
  }
</style>
