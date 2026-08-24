<script>
  import { postCommand } from '../api.js';
  import { formatUtcClock } from '../format.js';

  let { online, positions } = $props();

  const COMMANDS = [
    { name: 'status', label: 'status', hint: 'daemon status — no args' },
    { name: 'watch', label: 'watch', hint: 'wallet address' },
    { name: 'unwatch', label: 'unwatch', hint: 'wallet address' },
    { name: 'pause', label: 'pause', hint: 'target address' },
    { name: 'resume', label: 'resume', hint: 'target address' },
    { name: 'sell', label: 'sell', hint: 'market id' },
    { name: 'sell_half', label: 'sell_half', hint: 'market id' },
    { name: 'kill', label: 'kill', hint: 'kill switch — no args' },
  ];

  const ARG_COMMANDS = new Set([
    'watch',
    'unwatch',
    'pause',
    'resume',
    'sell',
    'sell_half',
  ]);

  let command = $state('status');
  let arg = $state('');
  let label = $state('');
  let busy = $state(false);
  let confirmArmed = $state(false);
  let feedback = $state([]);

  const current = $derived(COMMANDS.find((c) => c.name === command) ?? COMMANDS[0]);
  const needsArg = $derived(ARG_COMMANDS.has(current.name));
  const isSell = $derived(current.name === 'sell' || current.name === 'sell_half');
  const sellBlocked = $derived(isSell && positions.length === 0);
  const runDisabled = $derived(!online || busy || sellBlocked);

  function pushFeedback(item) {
    feedback = [
      {
        id: `cmd-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
        ts: new Date(),
        ...item,
      },
      ...feedback,
    ].slice(0, 5);
  }

  async function run() {
    if (busy || !online) return;

    const trimmed = arg.trim();

    // Sell exits are destructive: require a second, explicit confirmation.
    if (isSell && !confirmArmed) {
      confirmArmed = true;
      pushFeedback({
        name: current.name,
        ok: true,
        message: `${current.name} is an exit — press RUN again to confirm`,
      });
      return;
    }

    if (needsArg && !trimmed) {
      pushFeedback({
        name: current.name,
        ok: false,
        message: `${current.name} requires ${current.hint}`,
      });
      return;
    }

    const args =
      current.name === 'watch'
        ? label.trim()
          ? [trimmed, label.trim()]
          : [trimmed]
        : needsArg
          ? [trimmed]
          : [];

    busy = true;
    try {
      const res = await postCommand(current.name, args);
      pushFeedback({
        name: current.name,
        ok: Boolean(res?.ok),
        message: res?.message || (res?.ok ? 'ok' : 'command failed'),
      });
    } catch (err) {
      pushFeedback({ name: current.name, ok: false, message: err.message });
    } finally {
      busy = false;
      confirmArmed = false;
    }
  }

  function quick(name) {
    command = name;
    arg = '';
    label = '';
    confirmArmed = false;
    run();
  }

  function onCommandChange() {
    arg = '';
    label = '';
    confirmArmed = false;
  }

  function onArgKeydown(event) {
    if (event.key === 'Enter') run();
  }
</script>

<section class="panel panel-rail" aria-label="command rail">
  <header class="panel-head">
    <h2 class="panel-title"><span class="tick tick-blue"></span>Command Rail</h2>
    <span class="panel-meta mono">{online ? 'READY' : 'OFFLINE'}</span>
  </header>

  <div class="rail-body">
    <form class="rail-form" onsubmit={(e) => e.preventDefault()}>
      <select bind:value={command} onchange={onCommandChange} aria-label="command">
        {#each COMMANDS as cmd (cmd.name)}
          <option value={cmd.name}>{cmd.label}</option>
        {/each}
      </select>

      {#if needsArg}
        <input
          bind:value={arg}
          placeholder={current.hint}
          spellcheck="false"
          autocomplete="off"
          onkeydown={onArgKeydown}
          aria-label={current.hint}
        />
      {/if}

      {#if current.name === 'watch'}
        <input
          bind:value={label}
          placeholder="label (optional)"
          spellcheck="false"
          autocomplete="off"
          onkeydown={onArgKeydown}
          aria-label="target label"
        />
      {/if}

      <button
        class="btn {confirmArmed ? 'btn-confirm' : 'btn-accent'}"
        onclick={run}
        disabled={runDisabled}
      >
        {busy ? 'SENDING…' : confirmArmed ? 'CONFIRM' : 'RUN'}
      </button>
    </form>

    {#if sellBlocked}
      <div class="rail-hint tone-red">no open positions — sell disabled</div>
    {:else if current.name === 'kill'}
      <div class="rail-hint tone-amber">toggles the daemon kill switch</div>
    {:else}
      <div class="rail-hint">{current.hint}</div>
    {/if}

    <div class="rail-quick">
      <button class="btn" onclick={() => quick('status')} disabled={!online || busy}>
        STATUS
      </button>
      <button
        class="btn btn-danger"
        onclick={() => quick('kill')}
        disabled={!online || busy}
      >
        KILL SWITCH
      </button>
    </div>

    <div class="rail-feedback" aria-live="polite">
      {#each feedback as fb (fb.id)}
        <div class="fb-row {fb.ok ? 'fb-ok' : 'fb-err'}">
          <time>{formatUtcClock(fb.ts)}</time>
          <code>{fb.name}</code>
          <span>{fb.message}</span>
        </div>
      {/each}
      {#if !feedback.length}
        <div class="rail-hint">command results appear here</div>
      {/if}
    </div>
  </div>
</section>