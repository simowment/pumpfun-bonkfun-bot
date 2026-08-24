<script>
  import { formatEventTime } from '../format.js';

  let { events, online, loading } = $props();

  let listRef;
  let stickToBottom = $state(true);

  function onScroll() {
    const el = listRef;
    if (!el) return;
    stickToBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 56;
  }

  $effect(() => {
    events; // subscribe to feed changes
    const el = listRef;
    if (el && stickToBottom) el.scrollTop = el.scrollHeight;
  });
</script>

<section class="panel panel-feed" aria-label="live event feed">
  <header class="panel-head">
    <h2 class="panel-title"><span class="tick tick-blue"></span>Event Feed</h2>
    <div class="feed-meta">
      <span class="feed-live {online ? '' : 'idle'}">{online ? 'LIVE' : 'IDLE'}</span>
      <span class="panel-meta mono">{events.length} buffered</span>
    </div>
  </header>

  <div class="panel-body feed-scroll" bind:this={listRef} onscroll={onScroll}>
    {#if loading && !events.length}
      <div class="skeleton-rows" aria-hidden="true">
        <div class="skeleton-line"></div>
        <div class="skeleton-line"></div>
        <div class="skeleton-line"></div>
        <div class="skeleton-line"></div>
        <div class="skeleton-line"></div>
        <div class="skeleton-line"></div>
      </div>
    {:else if !events.length}
      <div class="empty">
        <div class="empty-title">Awaiting events</div>
        <div class="empty-body">
          The feed populates when the backend connects and the listener reports activity.
        </div>
      </div>
    {:else}
      <ol class="feed-list">
        {#each events as evt (evt.id)}
          <li class="feed-item">
            <time>{formatEventTime(evt.timestamp)}</time>
            <span class="badge badge-{evt.tone}">{evt.label}</span>
            <span class="feed-text" title={evt.text}>{evt.text}</span>
          </li>
        {/each}
      </ol>
      {#if !stickToBottom && events.length > 12}
        <button
          class="jump-btn"
          onclick={() => {
            stickToBottom = true;
          }}
        >
          ↓ jump to latest
        </button>
      {/if}
    {/if}
  </div>
</section>