<script>
  let { kpis } = $props();
</script>

<section class="kpi-strip" aria-label="overview">
  {#each kpis as kpi (kpi.key)}
    <div class="kpi-card" style={`--kpi-tone: var(--${kpi.tone})`}>
      <div class="kpi-label">
        <span class="tick tick-{kpi.tone}"></span>
        {kpi.label}
      </div>
      <div class="kpi-value">{kpi.value}</div>
      {#if kpi.hint}
        <div class="kpi-hint">{kpi.hint}</div>
      {/if}
    </div>
  {/each}
</section>