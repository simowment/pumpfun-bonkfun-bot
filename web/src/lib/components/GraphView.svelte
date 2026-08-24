<script>
  import { onDestroy, onMount } from 'svelte';
  import { GraphChart } from 'echarts/charts';
  import { TooltipComponent } from 'echarts/components';
  import * as echarts from 'echarts/core';
  import { CanvasRenderer } from 'echarts/renderers';

  echarts.use([GraphChart, TooltipComponent, CanvasRenderer]);

  let { entity = null, onSelectWallet = null } = $props();
  let container = $state(null);
  let addressFilter = $state('');
  let roleFilter = $state('ALL');
  let selectedAddress = $state('');
  let chart = null;
  let observer = null;
  const ROLE_PRIORITY = ['TARGET', 'REPEAT_BUNDLER', 'CREATOR', 'FUNDING_SOURCE', 'DIRECT_COUNTERPARTY', 'EXPANDED_COUNTERPARTY'];

  const graph = $derived.by(() => {
    const sourceNodes = Array.isArray(entity?.graph?.nodes) ? entity.graph.nodes : [];
    const sourceEdges = Array.isArray(entity?.graph?.edges) ? entity.graph.edges : [];
    const metrics = new Map(sourceNodes.map((node) => [node.address, { neighbors: new Set(), incoming: 0, outgoing: 0 }]));
    for (const edge of sourceEdges) {
      const source = metrics.get(edge.source);
      const target = metrics.get(edge.target);
      if (source) {
        source.neighbors.add(edge.target);
        source.outgoing += 1;
      }
      if (target) {
        target.neighbors.add(edge.source);
        target.incoming += 1;
      }
    }
    const query = addressFilter.trim().toLowerCase();
    const nodes = sourceNodes
      .map((node) => {
        const roles = Array.isArray(node.roles)
          ? node.roles.map((role) => String(role).toUpperCase())
          : [];
        const isBundler = roles.includes('REPEAT_BUNDLER');
        const role = primaryRole(node.is_target === true, roles);
        const nodeMetrics = metrics.get(node.address);
        const degree = nodeMetrics?.neighbors.size || 0;
        const isImportant = node.is_target || isBundler || ['CREATOR', 'FUNDING_SOURCE'].includes(role);
        return {
          id: node.address,
          name: node.address,
          address: node.address,
          roles,
          role,
          roleLabel: readableRole(role),
          isTarget: node.is_target === true,
          isBundler,
          isImportant,
          degree,
          incoming: nodeMetrics?.incoming || 0,
          outgoing: nodeMetrics?.outgoing || 0,
          scannedTransactions: node.scanned_transaction_count || 0,
          launchCount: node.launch_count || 0,
          firstSeenSlot: node.first_seen_slot ?? null,
          lastSeenSlot: node.last_seen_slot ?? null,
          freshness: node.fresh_wallet_status || 'unknown',
          symbolSize: node.is_target ? 82 : isBundler ? 68 : Math.min(54, 32 + degree * 2),
          itemStyle: {
            color: roleColor(role),
            borderColor: node.is_target ? '#fff' : isBundler ? '#ffb09f' : roleBorder(role),
            borderWidth: node.is_target || isBundler ? 2.5 : 1.5,
          },
        };
      })
      .filter((node) => roleFilter === 'ALL' || node.role === roleFilter || node.roles.includes(roleFilter))
      .filter((node) => !query || node.address.toLowerCase().includes(query) || node.roles.join(' ').toLowerCase().includes(query));
    const addresses = new Set(nodes.map((node) => node.address));
    return {
      nodes,
      links: sourceEdges
        .filter((edge) => addresses.has(edge.source) && addresses.has(edge.target))
        .map((edge) => ({
          source: edge.source,
          target: edge.target,
          relation: String(edge.kind || edge.asset_kind || 'OBSERVED').toUpperCase(),
          assetKind: edge.asset_kind || 'unknown',
          transferCount: edge.transfer_count || 1,
          amount: edge.amount_base_units || edge.amount_lamports || 0,
        })),
    };
  });
  const filterActive = $derived(addressFilter.trim().length > 0 || roleFilter !== 'ALL');
  const selectedNode = $derived(
    graph.nodes.find((node) => node.address === selectedAddress) ||
      graph.nodes.find((node) => node.isTarget) ||
      graph.nodes[0] ||
      null,
  );
  const rankedNodes = $derived(
    [...graph.nodes]
      .sort((left, right) => Number(right.isImportant) - Number(left.isImportant) || right.degree - left.degree || right.scannedTransactions - left.scannedTransactions)
      .slice(0, 8),
  );
  const roleCounts = $derived.by(() => {
    const counts = new Map();
    for (const node of graph.nodes) counts.set(node.role, (counts.get(node.role) || 0) + 1);
    return [...counts.entries()].sort((left, right) => right[1] - left[1]);
  });

  function primaryRole(isTarget, roles) {
    if (isTarget) return 'TARGET';
    return ROLE_PRIORITY.find((role) => roles.includes(role)) || roles[0] || 'LINKED';
  }

  function readableRole(role) {
    return role.replaceAll('_', ' ');
  }

  function roleColor(role) {
    if (role === 'TARGET') return '#ff8c00';
    if (role === 'REPEAT_BUNDLER') return '#ff5d43';
    if (role === 'CREATOR') return '#60c5ff';
    if (role === 'FUNDING_SOURCE') return '#10b981';
    if (role === 'DIRECT_COUNTERPARTY') return '#8b6d55';
    return '#201f1f';
  }

  function roleBorder(role) {
    if (role === 'CREATOR') return '#9cddff';
    if (role === 'FUNDING_SOURCE') return '#6ee7b7';
    if (role === 'DIRECT_COUNTERPARTY') return '#c7a98e';
    return '#806f63';
  }

  function short(address) {
    return `${address.slice(0, 4)}...${address.slice(-4)}`;
  }

  function selectNode(node) {
    selectedAddress = node.address;
    onSelectWallet?.({
      address: node.address,
      role: node.role,
      balance: null,
      confidence: null,
    });
  }

  function render(data) {
    if (!chart) return;
    chart.setOption({
      backgroundColor: '#0e0e0e',
      animation: false,
      tooltip: {
        trigger: 'item',
        backgroundColor: '#1c1b1b',
        borderColor: '#ff8c00',
        textStyle: { color: '#e5e2e1', fontFamily: 'monospace', fontSize: 12 },
        formatter: ({ data: item, dataType }) =>
          dataType === 'edge'
            ? `${readableRole(item.relation)}<br/>${item.transferCount} observed transfer${item.transferCount === 1 ? '' : 's'}`
            : `${item.roleLabel}<br/>${item.address}<br/>${item.degree} links · ${item.scannedTransactions} scanned tx<br/>${item.roles.map(readableRole).join(', ') || 'No classified role'}`,
      },
      series: [{
        type: 'graph',
        layout: 'force',
        roam: true,
        data: data.nodes,
        links: data.links,
        force: { repulsion: 360, edgeLength: 145, gravity: 0.06 },
        edgeSymbol: ['none', 'arrow'],
        edgeSymbolSize: 6,
        lineStyle: { color: '#806f63', width: 1.2, opacity: 0.65 },
        label: {
          show: true,
          position: 'bottom',
          distance: 5,
          color: '#f1eeeb',
          fontFamily: 'monospace',
          fontSize: 11,
          formatter: ({ data: item }) =>
            item.isImportant
              ? `{role|${item.roleLabel}}\n{addr|${short(item.address)}}\n{meta|${item.degree} links · ${item.scannedTransactions} tx}`
              : `{addr|${short(item.address)}}\n{meta|${item.degree}L}`,
          rich: {
            role: { color: '#ffb45a', fontSize: 9, fontWeight: 800, lineHeight: 12 },
            addr: { color: '#f1eeeb', fontSize: 10, fontWeight: 700, lineHeight: 12 },
            meta: { color: '#a99c92', fontSize: 8, lineHeight: 10 },
          },
        },
        emphasis: { focus: 'adjacency', scale: 1.15 },
      }],
    }, true);
    chart.off('click');
    chart.on('click', ({ data, dataType }) => {
      if (dataType === 'node') selectNode(data);
    });
  }

  $effect(() => render(graph));

  onMount(() => {
    chart = echarts.init(container, null, { renderer: 'canvas' });
    render(graph);
    observer = new ResizeObserver(() => chart?.resize());
    observer.observe(container);
  });

  onDestroy(() => {
    observer?.disconnect();
    chart?.dispose();
  });
</script>

<div class="card">
  <header>
    <div><b>LIVE GRAPH</b> FINALIZED ENTITY CONNECTIONS <span>[{graph.nodes.length} NODES / {graph.links.length} EDGES]</span></div>
    <div class="filters">
      <input bind:value={addressFilter} placeholder="Filter address or role" aria-label="Filter entity addresses" />
      <select bind:value={roleFilter} aria-label="Filter entity role">
        <option value="ALL">ALL ROLES</option>
        <option value="TARGET">TARGET</option>
        <option value="REPEAT_BUNDLER">REPEAT BUNDLERS</option>
        <option value="CREATOR">CREATORS</option>
        <option value="LINKED">LINKED</option>
      </select>
      {#if filterActive}<button onclick={() => { addressFilter = ''; roleFilter = 'ALL'; }}>CLEAR</button>{/if}
    </div>
  </header>

  {#if filterActive}
    <div class="address-results">
      {#each graph.nodes as node (node.address)}
        <button onclick={() => selectNode(node)} title={node.address}>
          <strong>{node.role}</strong><span>{node.address}</span>
        </button>
      {:else}
        <p>No address matches this filter.</p>
      {/each}
    </div>
  {/if}

  <div class="graph-readout">
    <div class="role-summary">
      {#each roleCounts as [role, count] (role)}
        <span><i style:background={roleColor(role)}></i>{readableRole(role)} <b>{count}</b></span>
      {/each}
    </div>
    {#if selectedNode}
      <button class="selected-node" onclick={() => selectNode(selectedNode)} title={selectedNode.address}>
        <strong>{selectedNode.roleLabel}</strong>
        <code>{selectedNode.address}</code>
        <span>{selectedNode.degree} LINKS</span>
        <span>{selectedNode.scannedTransactions} TX</span>
        <span>{selectedNode.launchCount} LAUNCHES</span>
        <span>{selectedNode.incoming} IN / {selectedNode.outgoing} OUT</span>
      </button>
    {/if}
  </div>

  <div class="canvas">
    {#if graph.nodes.length === 0}<div class="empty">{filterActive ? 'No matching entity address.' : 'Scan a wallet or mint to load finalized graph evidence.'}</div>{/if}
    <div class={['chart', graph.nodes.length === 0 && 'hidden']} bind:this={container}></div>
    <div class="legend"><i></i> TARGET <i class="bundler"></i> REPEAT BUNDLER <i class="linked"></i> LINKED</div>
  </div>
  <div class="node-index">
    <b>TOP CONNECTED</b>
    {#each rankedNodes as node (node.address)}
      <button class={node.address === selectedNode?.address ? 'selected' : ''} onclick={() => selectNode(node)} title={node.address}>
        <span>{node.roleLabel}</span><code>{short(node.address)}</code><em>{node.degree}L · {node.scannedTransactions}TX</em>
      </button>
    {/each}
  </div>
</div>

<style>
  .card { border: 1.5px solid var(--outline-variant); background: var(--surface-container-lowest); }
  header { display: flex; justify-content: space-between; align-items: center; gap: 8px; flex-wrap: wrap; padding: 7px 10px; border-bottom: 1.5px solid var(--outline-variant); background: var(--surface-container-low); color: var(--stark-white); font: 900 13px var(--font-headline); }
  header b { margin-right: 8px; padding: 2px 6px; background: var(--primary-container); color: #000; font: 800 9.5px var(--font-meta); }
  header span { margin-left: 8px; color: var(--tertiary); font: 700 10px var(--font-meta); }
  .filters { display: flex; gap: 4px; }
  .filters input, .filters select, .filters button { height: 26px; padding: 3px 7px; background: #111; border: 1px solid var(--outline-variant); color: var(--on-surface); font: 10px var(--font-data); }
  .filters input { width: 190px; }
  .filters button { color: var(--primary-container); cursor: pointer; }
  .address-results { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); max-height: 130px; overflow-y: auto; border-bottom: 1px solid var(--outline-variant); }
  .address-results button { display: grid; grid-template-columns: 105px minmax(0, 1fr); gap: 5px; padding: 5px 8px; text-align: left; background: #111; border: 0; border-right: 1px solid var(--outline-variant); border-bottom: 1px solid var(--outline-variant); color: var(--on-surface); cursor: pointer; }
  .address-results button:hover { background: var(--surface-container-high); }
  .address-results strong { color: var(--primary-container); font: 800 9px var(--font-meta); }
  .address-results span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font: 10px var(--font-data); }
  .address-results p { padding: 6px 9px; color: var(--on-surface-variant); font: 10px var(--font-data); }
  .graph-readout { display: grid; grid-template-columns: minmax(0, 1fr) minmax(420px, auto); gap: 6px; padding: 5px 7px; border-bottom: 1px solid var(--outline-variant); background: #111; }
  .role-summary { display: flex; align-items: center; gap: 5px; overflow-x: auto; }
  .role-summary span { display: inline-flex; flex: 0 0 auto; align-items: center; gap: 4px; padding: 3px 5px; border: 1px solid var(--outline-variant); color: var(--on-surface-variant); font: 8px var(--font-meta); text-transform: uppercase; }
  .role-summary i { width: 6px; height: 6px; margin: 0; }
  .role-summary b { color: var(--stark-white); }
  .selected-node { display: grid; grid-template-columns: auto minmax(120px, 1fr) repeat(4, auto); align-items: center; gap: 7px; min-width: 0; padding: 4px 7px; background: var(--surface-container-low); border: 1px solid var(--primary-container); color: var(--on-surface); text-align: left; }
  .selected-node strong { color: var(--primary-container); font: 800 8px var(--font-meta); }
  .selected-node code { overflow: hidden; color: var(--stark-white); font: 9px var(--font-data); text-overflow: ellipsis; }
  .selected-node span { color: var(--on-surface-variant); font: 8px var(--font-meta); white-space: nowrap; }
  .canvas { position: relative; height: 500px; background: #0e0e0e; overflow: hidden; }
  .chart { width: 100%; height: 100%; }
  .hidden { visibility: hidden; }
  .empty { position: absolute; inset: 0; display: grid; place-items: center; color: var(--on-surface-variant); font: 11px var(--font-data); }
  .legend { position: absolute; right: 12px; bottom: 12px; padding: 5px 8px; background: #0e0e0ee6; border: 1px solid var(--outline-variant); color: var(--on-surface-variant); font: 700 9px var(--font-meta); }
  i { display: inline-block; width: 8px; height: 8px; margin: 0 4px; background: var(--primary-container); }
  i.bundler { background: #ff5d43; }
  i.linked { background: #806f63; }
  .node-index { display: grid; grid-template-columns: auto repeat(4, minmax(125px, 1fr)); gap: 3px; padding: 5px 7px; border-top: 1px solid var(--outline-variant); background: #111; }
  .node-index > b { display: grid; align-items: center; padding: 0 5px; color: var(--on-surface-variant); font: 800 8px var(--font-meta); }
  .node-index button { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 1px 5px; min-width: 0; padding: 4px 6px; background: #171717; border: 1px solid var(--outline-variant); text-align: left; }
  .node-index button.selected { border-color: var(--primary-container); background: #21180e; }
  .node-index span { overflow: hidden; color: var(--on-surface-variant); font: 7px var(--font-meta); text-overflow: ellipsis; white-space: nowrap; }
  .node-index code { color: var(--stark-white); font: 8px var(--font-data); }
  .node-index em { grid-column: span 2; color: var(--tertiary); font: 7px var(--font-meta); font-style: normal; }
  @media (max-width: 1100px) {
    .graph-readout { grid-template-columns: 1fr; }
    .selected-node { grid-template-columns: auto minmax(100px, 1fr) repeat(2, auto); }
    .node-index { grid-template-columns: auto repeat(2, minmax(125px, 1fr)); }
  }
</style>
