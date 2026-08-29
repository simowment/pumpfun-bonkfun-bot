<script>
  let {
    rootWallet = '—', rawAddress = '', linkedWallets = 0, creatorWallets = 0,
    bundleWallets = 0, fundingHubs = 0, launchesCount = 0,
  } = $props();
  let copied = $state(false);

  function copyRoot() {
    if (!rawAddress) return;
    navigator.clipboard.writeText(rawAddress);
    copied = true;
    setTimeout(() => (copied = false), 2000);
  }
</script>

<div class="strip">
  <div><small>[ ROOT DEPLOYER ]</small><p>{rootWallet}<button onclick={copyRoot} disabled={!rawAddress}>{copied ? '✓' : '⧉'}</button></p></div>
  <div><small>[ LINKED NODES ]</small><p class="ice">{linkedWallets}<em>GRAPH</em></p></div>
  <div><small>[ CREATOR DEVS ]</small><p>{creatorWallets}<em>FINALIZED</em></p></div>
  <div><small>[ BUNDLERS ]</small><p>{bundleWallets}<em>REPEAT</em></p></div>
  <div><small>[ GAS HUBS ]</small><p>{fundingHubs}<em>OBSERVED</em></p></div>
  <div><small>[ ON-CHAIN LAUNCHES ]</small><p class="green">{launchesCount}<em>FINALIZED</em></p></div>
</div>

<style>
  .strip { display: grid; grid-template-columns: repeat(6, 1fr); gap: 6px; margin-bottom: 6px; }
  .strip > div { padding: 8px 10px; background: var(--surface-container-low); border: 1px solid var(--outline-variant); }
  small { color: var(--on-surface-variant); font: 700 9.5px var(--font-meta); }
  p { display: flex; justify-content: space-between; align-items: center; margin: 4px 0 0; color: var(--stark-white); font: 800 14px var(--font-data); }
  em, button { padding: 2px 4px; background: var(--surface-container-high); border: 1px solid var(--outline-variant); color: var(--on-surface-variant); font: normal 9px var(--font-meta); }
  button { cursor: pointer; }
  button:disabled { opacity: .45; }
  .ice { color: var(--tertiary); } .green { color: var(--green-ok); }
  @media (max-width: 1100px) { .strip { grid-template-columns: repeat(3, 1fr); } }
  @media (max-width: 650px) { .strip { grid-template-columns: repeat(2, 1fr); } }
</style>
