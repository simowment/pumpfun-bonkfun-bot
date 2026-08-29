<script>
  import { onMount, onDestroy } from 'svelte';
  import * as echarts from 'echarts';

  let {
    cluster = null,
    entity = null,
    onSelectWallet = null,
  } = $props();

  let chartContainer = $state(null);
  let chart = null;
  let resizeObserver = null;

  // Filter state
  let depth = $state(3);
  let minConfidence = $state(70);
  let showBundlers = $state(true);
  let showFunders = $state(true);
  let showCreators = $state(true);
  let showCex = $state(true);

  // Interactive Visual Settings
  let showVisualSettings = $state(false);
  let nodeSpacing = $state(200);       // Edge length: 120 - 360
  let nodeScale = $state(1.0);         // Node scale: 0.6 - 1.4
  let showEdgeLabels = $state(true);   // Toggle arrow labels
  let repulsionForce = $state(850);    // Physics spread: 400 - 1600
  let pinMovedNodes = $state(true);    // Keep bubbles where dropped

  // Store user-pinned positions across re-renders
  let pinnedPositions = new Map();

  function shortAddr(a) {
    if (!a || a.length < 10) return a || '—';
    return `${a.slice(0, 4)}...${a.slice(-4)}`;
  }

  function generateBubbleMapData() {
    const nodes = [];
    const links = [];
    const nodeSet = new Set();

    function addBubble(id, name, role, balance, bgColor, borderColor, textColor, baseSize, category) {
      if (!nodeSet.has(id)) {
        nodeSet.add(id);
        const finalSize = Math.round(baseSize * nodeScale);
        const savedPos = pinnedPositions.get(id);

        const nodeObj = {
          id,
          name: `${role}\n${name}\n${balance} SOL`,
          rawAddress: id,
          role,
          balance,
          symbolSize: finalSize,
          category,
          fixed: pinMovedNodes && savedPos ? true : false,
          x: savedPos ? savedPos.x : undefined,
          y: savedPos ? savedPos.y : undefined,
          itemStyle: {
            color: bgColor,
            borderColor: borderColor,
            borderWidth: 2,
            shadowBlur: 0,
          },
          label: {
            show: true,
            position: 'inside',
            formatter: `${role}\n${name}\n{c|${balance} SOL}`,
            rich: {
              c: {
                color: textColor === '#000000' || textColor === '#4d2600' ? '#623200' : '#60c5ff',
                fontWeight: 'bold',
                fontSize: Math.round(11 * nodeScale),
                lineHeight: Math.round(15 * nodeScale),
              },
            },
            color: textColor,
            fontSize: Math.round(11.5 * nodeScale),
            fontWeight: '600',
            fontFamily: 'Inter, system-ui, sans-serif',
          },
        };
        nodes.push(nodeObj);
      }
    }

    function formatEdgeLabel(raw) {
      if (!raw) return '';
      if (raw === 'direct_native_transfer') return 'SOL';
      if (raw === 'direct_spl_transfer') return 'SPL';
      if (raw.includes('B0') || raw.includes('BUNDLE')) return 'B0';
      if (raw.includes('SYNDICATE') || raw.includes('CROSS')) return 'SYN';
      return raw.length > 8 ? `${raw.slice(0, 7)}…` : raw;
    }

    function addEdge(source, target, label, strokeColor = '#5c4d40', width = 1.5) {
      if (nodeSet.has(source) && nodeSet.has(target)) {
        const compactLabel = formatEdgeLabel(label);
        links.push({
          source,
          target,
          value: label,
          lineStyle: {
            color: strokeColor,
            width,
            type: 'dashed',
            curveness: 0.08,
          },
          label: {
            show: showEdgeLabels,
            formatter: compactLabel,
            fontSize: 8,
            fontWeight: '600',
            color: '#b5a494',
            backgroundColor: 'rgba(14, 14, 14, 0.7)',
            padding: [1, 2],
            borderRadius: 2,
            fontFamily: 'Inter, system-ui, sans-serif',
          },
          emphasis: {
            label: {
              show: true,
              formatter: label,
              fontSize: 10,
              fontWeight: 'bold',
              color: '#ffffff',
              backgroundColor: 'rgba(28, 27, 27, 0.95)',
              borderColor: '#ff8c00',
              borderWidth: 1,
              padding: [2, 5],
            },
            lineStyle: {
              width: width * 1.8,
              color: '#ff8c00',
            },
          },
        });
      }
    }

    // 1. Direct on-chain graph nodes from entity analysis
    const graphNodes = cluster?.graph?.nodes || entity?.graph?.nodes || [];
    const graphLinks = cluster?.graph?.links || entity?.graph?.links || cluster?.graph?.edges || entity?.graph?.edges || [];

    if (graphNodes.length > 0) {
      graphNodes.forEach((node) => {
        const addr = node.address;
        if (!addr) return;
        const roles = node.roles || [];
        const isTarget = node.is_target || roles.includes('target');
        const isMaster = isTarget || roles.includes('MASTER_DEPLOYER') || roles.includes('PRIMARY_CREATOR');
        const isFunder = roles.includes('FUNDER_HUB') || roles.includes('GAS_FUNDER') || roles.includes('funding_source');
        const isBundler = roles.includes('SATELLITE_BUNDLER') || roles.includes('BUNDLE_BUYER');
        const isCreator = roles.includes('SUB_DEPLOYER') || roles.includes('CO_CREATOR');
        const isStorage = roles.includes('TREASURY_SWEEP') || roles.includes('STORAGE_SWEEP');

        if (isBundler && !showBundlers) return;
        if (isFunder && !showFunders) return;
        if (isCreator && !showCreators) return;

        let roleLabel = 'NODE';
        let bgColor = '#201f1f';
        let borderColor = '#3e332a';
        let textColor = '#f1ede8';
        let size = 56;
        let cat = 5;

        if (isMaster) {
          roleLabel = 'ROOT DEV';
          bgColor = '#ff8c00';
          borderColor = '#ffffff';
          textColor = '#000000';
          size = 88;
          cat = 0;
        } else if (isFunder) {
          roleLabel = 'GAS HUB';
          bgColor = '#ffb77d';
          borderColor = '#ff8c00';
          textColor = '#4d2600';
          size = 78;
          cat = 1;
        } else if (isCreator) {
          roleLabel = 'SUB DEV';
          bgColor = '#ff5625';
          borderColor = '#ffffff';
          textColor = '#ffffff';
          size = 70;
          cat = 2;
        } else if (isStorage) {
          roleLabel = 'STORAGE';
          bgColor = '#60c5ff';
          borderColor = '#00b5fc';
          textColor = '#00344c';
          size = 74;
          cat = 3;
        } else if (isBundler) {
          roleLabel = 'BUNDLER';
          bgColor = '#ffb5a0';
          borderColor = '#ff5625';
          textColor = '#601400';
          size = 64;
          cat = 4;
        }

        const balText = node.balance_sol != null ? `${node.balance_sol.toFixed(2)}` : (node.launch_count != null ? `${node.launch_count} M` : '—');
        addBubble(addr, shortAddr(addr), roleLabel, balText, bgColor, borderColor, textColor, size, cat);
      });

      graphLinks.forEach((link) => {
        if (link.source && link.target) {
          addEdge(
            link.source,
            link.target,
            link.label || link.type || link.kind || link.asset_id || '—',
            '#5c4d40',
            1.5
          );
        }
      });

      // 2. Cross-Entity Bundlers & Launch Bundles Integration
      const targetDev = entity?.target_wallet || entity?.address || entity?.tracking_address || (graphNodes.find((n) => n.is_target)?.address) || '';

      // Add launch bundles (B0 bundle buyers)
      const launchBundles = entity?.launch_bundles?.launches || [];
      launchBundles.forEach((lb) => {
        if (lb.first_buyer_wallet && showBundlers) {
          const bAddr = lb.first_buyer_wallet;
          addBubble(
            bAddr,
            shortAddr(bAddr),
            'BUNDLER',
            lb.bundle_size ? `${lb.bundle_size} B0` : 'B0',
            '#c084fc',
            '#a855f7',
            '#3b0764',
            66,
            4
          );
          if (targetDev) {
            addEdge(bAddr, targetDev, 'B0 BUNDLE', '#a855f7', 2);
          }
        }
      });

      // Add cross-entity bundlers connecting multiple creators
      const crossBundles = entity?.cross_entity_bundles?.wallets || [];
      crossBundles.forEach((cb) => {
        if (cb.wallet && showBundlers) {
          const bAddr = cb.wallet;
          addBubble(
            bAddr,
            shortAddr(bAddr),
            'CROSS BUNDLER',
            `${cb.external_creator_count || 1} DEVS`,
            '#e879f9',
            '#d946ef',
            '#4a044e',
            72,
            4
          );
          if (targetDev) {
            addEdge(bAddr, targetDev, 'SYNDICATE BUNDLE', '#d946ef', 2.5);
          }
          (cb.external_creators || []).forEach((extCreator) => {
            if (extCreator && showCreators) {
              addBubble(
                extCreator,
                shortAddr(extCreator),
                'LINKED CREATOR',
                '—',
                '#fb923c',
                '#ea580c',
                '#431407',
                74,
                2
              );
              addEdge(bAddr, extCreator, 'CROSS BUNDLE', '#d946ef', 2);
            }
          });
        }
      });
    } else {
      // Single root deployer if no graph yet
      const masterAddr = cluster?.master_deployer?.address || entity?.address || entity?.tracking_address || entity?.target_wallet || '';
      if (masterAddr) {
        addBubble(
          masterAddr,
          shortAddr(masterAddr),
          'ROOT DEV',
          '—',
          '#ff8c00',
          '#ffffff',
          '#000000',
          88,
          0
        );
      }
    }

    return { nodes, links };
  }

  function initChart() {
    if (!chartContainer) return;

    try {
      if (chart) chart.dispose();
      chart = echarts.init(chartContainer);

      const { nodes, links } = generateBubbleMapData();

      const option = {
        backgroundColor: '#0e0e0e',
        tooltip: {
          trigger: 'item',
          backgroundColor: '#1c1b1b',
          borderColor: '#ff8c00',
          borderWidth: 1,
          textStyle: {
            color: '#f1ede8',
            fontSize: 13,
            fontFamily: 'Inter, system-ui, sans-serif',
          },
          formatter: function (params) {
            if (params.dataType === 'edge') {
              return `<b>${params.data.value || 'Link'}</b><br/>From: ${shortAddr(params.data.source)}<br/>To: ${shortAddr(params.data.target)}`;
            }
            const d = params.data;
            return `<b>${d.role}</b><br/>
                    Address: <span style="color:#ffb77d;font-family:monospace;">${d.rawAddress}</span><br/>
                    Balance: <span style="color:#60c5ff;font-weight:bold;">${d.balance} SOL</span>`;
          },
        },
        animationDuration: 500,
        animationEasingUpdate: 'quinticInOut',
        series: [
          {
            type: 'graph',
            layout: 'force',
            data: nodes,
            links: links,
            roam: true,
            draggable: true,
            zoom: 0.95,
            force: {
              repulsion: repulsionForce,
              edgeLength: [Math.round(nodeSpacing * 0.75), Math.round(nodeSpacing * 1.25)],
              gravity: 0.08,
              friction: 0.90,
            },
            emphasis: {
              focus: 'adjacency',
              lineStyle: {
                width: 2.5,
              },
            },
            edgeSymbol: ['none', 'arrow'],
            edgeSymbolSize: 8,
          },
        ],
      };

      chart.setOption(option);

      // Save dragged node positions so they stay in place when moved
      chart.on('mouseup', function (params) {
        if (params.dataType === 'node' && pinMovedNodes) {
          const d = params.data;
          pinnedPositions.set(d.rawAddress, { x: d.x, y: d.y });
        }
      });

      chart.on('click', (params) => {
        if (params.dataType === 'node' && onSelectWallet) {
          const d = params.data;
          onSelectWallet({
            address: d.rawAddress,
            role: d.role,
            balance: d.balance,
            confidence: null,
          });
        }
      });
    } catch (e) {
      console.error('Error initializing bubble map chart:', e);
    }
  }

  function zoomIn() {
    if (chart) {
      const opt = chart.getOption();
      const currentZoom = opt.series[0].zoom || 1.0;
      chart.setOption({
        series: [{ zoom: currentZoom * 1.25 }],
      });
    }
  }

  function zoomOut() {
    if (chart) {
      const opt = chart.getOption();
      const currentZoom = opt.series[0].zoom || 1.0;
      chart.setOption({
        series: [{ zoom: Math.max(0.3, currentZoom * 0.8) }],
      });
    }
  }

  function fitScreen() {
    if (chart) {
      chart.setOption({
        series: [{ zoom: 0.95, center: null }],
      });
      chart.resize();
    }
  }

  function resetPositions() {
    pinnedPositions.clear();
    nodeSpacing = 200;
    nodeScale = 1.0;
    showEdgeLabels = true;
    repulsionForce = 850;
    initChart();
  }

  // Reactive updates on settings changes
  $effect(() => {
    if ((cluster || entity) && chartContainer) {
      initChart();
    }
  });

  $effect(() => {
    if (chart && (
      showBundlers !== undefined ||
      showFunders !== undefined ||
      showCreators !== undefined ||
      showCex !== undefined ||
      nodeSpacing !== undefined ||
      nodeScale !== undefined ||
      showEdgeLabels !== undefined ||
      repulsionForce !== undefined ||
      pinMovedNodes !== undefined
    )) {
      initChart();
    }
  });

  onMount(() => {
    initChart();

    if (chartContainer && typeof ResizeObserver !== 'undefined') {
      resizeObserver = new ResizeObserver(() => {
        if (chart) chart.resize();
      });
      resizeObserver.observe(chartContainer);
    }
  });

  onDestroy(() => {
    if (resizeObserver) resizeObserver.disconnect();
    if (chart) chart.dispose();
  });
</script>

<div class="technical-map-card">
  <!-- Controls Bar Header -->
  <div class="graph-toolbar">
    <div class="toolbar-left">
      <span class="graph-title">Cluster Network Graph</span>
      <span class="drag-hint">💡 Drag bubbles to reposition</span>
      <button
        class="settings-toggle-btn {showVisualSettings ? 'active' : ''}"
        onclick={() => (showVisualSettings = !showVisualSettings)}
        title="Visual Settings"
      >
        ⚙ Visual Settings
      </button>
    </div>

    <div class="filter-controls">
      <div class="toggle-group">
        <label class="toggle-label">
          <input type="checkbox" bind:checked={showBundlers} />
          <span>Bundlers</span>
        </label>
        <label class="toggle-label">
          <input type="checkbox" bind:checked={showFunders} />
          <span>Funders</span>
        </label>
        <label class="toggle-label">
          <input type="checkbox" bind:checked={showCreators} />
          <span>Creators</span>
        </label>
        <label class="toggle-label">
          <input type="checkbox" bind:checked={showCex} />
          <span>CEX</span>
        </label>
      </div>
    </div>
  </div>

  <!-- Interactive Visual Settings Drawer -->
  {#if showVisualSettings}
    <div class="visual-settings-drawer">
      <div class="setting-item">
        <label for="spacing-slider">Distance: <strong>{nodeSpacing}px</strong></label>
        <input
          id="spacing-slider"
          type="range"
          min="120"
          max="360"
          step="10"
          bind:value={nodeSpacing}
        />
      </div>

      <div class="setting-item">
        <label for="scale-slider">Node Size: <strong>{Math.round(nodeScale * 100)}%</strong></label>
        <input
          id="scale-slider"
          type="range"
          min="0.6"
          max="1.4"
          step="0.05"
          bind:value={nodeScale}
        />
      </div>

      <div class="setting-item">
        <label for="spread-slider">Repulsion: <strong>{repulsionForce}</strong></label>
        <input
          id="spread-slider"
          type="range"
          min="400"
          max="1600"
          step="50"
          bind:value={repulsionForce}
        />
      </div>

      <div class="setting-checkboxes">
        <label class="toggle-label">
          <input type="checkbox" bind:checked={showEdgeLabels} />
          <span>Arrow Labels</span>
        </label>
        <label class="toggle-label">
          <input type="checkbox" bind:checked={pinMovedNodes} />
          <span>Pin Moved Bubbles</span>
        </label>
      </div>

      <button class="btn-reset" onclick={resetPositions}>Reset Positions</button>
    </div>
  {/if}

  <!-- Bounded Responsive Network Canvas -->
  <div class="canvas-wrapper">
    <div class="echarts-container" bind:this={chartContainer}></div>

    <!-- Floating Zoom Controls on Left -->
    <div class="floating-zoom-controls">
      <button class="zoom-btn" onclick={zoomIn} title="Zoom In">+</button>
      <button class="zoom-btn" onclick={zoomOut} title="Zoom Out">−</button>
      <button class="zoom-btn fit-btn" onclick={fitScreen} title="Fit to Screen">⛶</button>
    </div>

    <!-- Bubble Legend Overlay -->
    <div class="bubble-legend">
      <div class="legend-item"><span class="legend-square orange"></span> Root Dev</div>
      <div class="legend-item"><span class="legend-square gold"></span> Gas Hub</div>
      <div class="legend-item"><span class="legend-square coral"></span> Bundler</div>
      <div class="legend-item"><span class="legend-square ice"></span> Storage</div>
    </div>
  </div>
</div>

<style>
  .technical-map-card {
    background-color: var(--surface-container-lowest);
    border: 1px solid var(--outline-variant);
    display: flex;
    flex-direction: column;
    width: 100%;
  }
  .graph-toolbar {
    padding: 8px 12px;
    background-color: var(--surface-container-low);
    border-bottom: 1px solid var(--outline-variant);
    display: flex;
    justify-content: space-between;
    align-items: center;
    flex-wrap: wrap;
    gap: 8px;
  }
  .toolbar-left {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
  }
  .graph-title {
    font-family: var(--font-sans);
    font-size: 14px;
    font-weight: 700;
    color: var(--stark-white);
  }
  .drag-hint {
    font-size: 11.5px;
    color: var(--primary-container);
    background-color: var(--surface-container-high);
    padding: 2px 6px;
    border-radius: 3px;
    font-weight: 500;
  }
  .settings-toggle-btn {
    background-color: var(--surface-container-high);
    border: 1px solid var(--outline-variant);
    color: var(--on-surface);
    font-family: var(--font-sans);
    font-size: 12px;
    font-weight: 600;
    padding: 4px 10px;
    cursor: pointer;
    border-radius: 3px;
    transition: all 0.15s;
  }
  .settings-toggle-btn:hover, .settings-toggle-btn.active {
    background-color: var(--primary-container);
    color: #000000;
    border-color: var(--stark-white);
  }
  .visual-settings-drawer {
    background-color: var(--surface-container);
    border-bottom: 1px solid var(--outline-variant);
    padding: 8px 14px;
    display: flex;
    align-items: center;
    gap: 16px;
    flex-wrap: wrap;
    font-size: 12px;
    font-family: var(--font-sans);
  }
  .setting-item {
    display: flex;
    flex-direction: column;
    gap: 3px;
  }
  .setting-item label {
    color: var(--on-surface-variant);
    font-size: 11px;
  }
  .setting-item label strong {
    color: var(--primary-container);
  }
  .setting-item input[type="range"] {
    width: 110px;
    accent-color: var(--primary-container);
    cursor: pointer;
  }
  .setting-checkboxes {
    display: flex;
    align-items: center;
    gap: 12px;
  }
  .btn-reset {
    background: transparent;
    border: 1px solid var(--outline-variant);
    color: var(--on-surface-variant);
    font-size: 11.5px;
    padding: 4px 8px;
    cursor: pointer;
    border-radius: 3px;
    margin-left: auto;
  }
  .btn-reset:hover {
    color: var(--stark-white);
    border-color: var(--stark-white);
  }
  .filter-controls {
    display: flex;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;
  }
  .toggle-group {
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .toggle-label {
    display: flex;
    align-items: center;
    gap: 5px;
    font-size: 12px;
    font-family: var(--font-sans);
    font-weight: 600;
    color: var(--on-surface-variant);
    cursor: pointer;
  }
  .toggle-label input {
    cursor: pointer;
    accent-color: var(--primary-container);
    width: 13px;
    height: 13px;
  }
  .canvas-wrapper {
    position: relative;
    width: 100%;
    height: 480px;
    background-color: #0e0e0e;
    overflow: hidden;
  }
  .echarts-container {
    width: 100%;
    height: 100%;
  }
  .floating-zoom-controls {
    position: absolute;
    top: 12px;
    left: 12px;
    display: flex;
    flex-direction: column;
    gap: 4px;
    z-index: 10;
  }
  .zoom-btn {
    width: 30px;
    height: 30px;
    background-color: var(--surface-container-high);
    color: var(--stark-white);
    border: 1px solid var(--outline-variant);
    font-size: 15px;
    font-weight: bold;
    display: flex;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    padding: 0;
    border-radius: 3px;
  }
  .zoom-btn:hover {
    background-color: var(--primary-container);
    color: #000000;
    border-color: var(--stark-white);
  }
  .fit-btn {
    font-size: 13px;
  }
  .bubble-legend {
    position: absolute;
    bottom: 12px;
    right: 12px;
    background-color: rgba(14, 14, 14, 0.9);
    border: 1px solid var(--outline-variant);
    padding: 5px 10px;
    display: flex;
    gap: 12px;
    font-size: 11.5px;
    font-family: var(--font-sans);
    font-weight: 600;
    color: var(--on-surface-variant);
    border-radius: 3px;
    z-index: 10;
  }
  .legend-item {
    display: flex;
    align-items: center;
    gap: 5px;
  }
  .legend-square {
    width: 9px;
    height: 9px;
    border-radius: 2px;
  }
  .legend-square.orange {
    background-color: var(--primary-container);
  }
  .legend-square.gold {
    background-color: var(--surface-tint);
  }
  .legend-square.coral {
    background-color: var(--secondary-container);
  }
  .legend-square.ice {
    background-color: var(--tertiary);
  }
</style>
