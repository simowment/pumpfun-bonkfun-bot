/**
 * Pure formatting and event-description helpers for the console.
 * Mirrors the TUI formatters (short_address, format_sol, format_age) so the
 * browser and terminal views read identically.
 */

export const LAMPORTS_PER_SOL = 1_000_000_000;

/** Shorten a long address/signature to head...tail. */
export function shortId(value, head = 6, tail = 6) {
  if (value == null) return '—';
  const s = String(value);
  if (s.length <= head + tail + 1) return s;
  return `${s.slice(0, head)}…${s.slice(-tail)}`;
}

/** Format exact lamports as a readable SOL string. */
export function formatSol(lamports) {
  if (lamports == null || !Number.isFinite(Number(lamports))) return '—';
  const sol = Number(lamports) / LAMPORTS_PER_SOL;
  return sol.toLocaleString('en-US', { maximumFractionDigits: 4 });
}

/** Format a parts-per-million PnL value as a signed percentage (10000 ppm = 1%). */
export function formatPpm(ppm, digits = 1) {
  if (ppm == null || !Number.isFinite(Number(ppm))) return '—';
  const pct = Number(ppm) / 10_000;
  const sign = pct > 0 ? '+' : pct < 0 ? '-' : '';
  return `${sign}${pct.toFixed(digits)}%`;
}

/** Relative age like the TUI: 0s, 45s, 2m, 1h 12m, 3d. */
export function formatAge(timestamp, nowTimestamp) {
  if (timestamp == null || Number(timestamp) <= 0) return '—';
  const now = nowTimestamp ?? Math.floor(Date.now() / 1000);
  const elapsed = Math.max(0, now - Number(timestamp));
  if (elapsed < 60) return `${elapsed}s`;
  if (elapsed < 3600) return `${Math.floor(elapsed / 60)}m`;
  if (elapsed < 86400) {
    const hours = Math.floor(elapsed / 3600);
    const mins = Math.floor((elapsed % 3600) / 60);
    return mins ? `${hours}h ${mins}m` : `${hours}h`;
  }
  return `${Math.floor(elapsed / 86400)}d`;
}

/** UTC clock HH:MM:SS. */
export function formatUtcClock(date) {
  if (!(date instanceof Date) || Number.isNaN(date.getTime())) return '--:--:--';
  return date.toISOString().slice(11, 19);
}

/** UTC clock from a unix-seconds timestamp. */
export function formatEventTime(timestamp) {
  if (timestamp == null || !Number.isFinite(Number(timestamp)) || Number(timestamp) <= 0) {
    return '--:--:--';
  }
  return formatUtcClock(new Date(Number(timestamp) * 1000));
}

/* --- Live event description ------------------------------------------------ */

const EVENT_LABELS = {
  launch_detected: 'LAUNCH',
  funder_added: 'FUNDER',
  wallet_funded: 'FUNDED',
  transfer_detected: 'TRANSFER',
  wallet_expired: 'EXPIRED',
  path_depth_limit_reached: 'DEPTH',
  path_stopped: 'STOPPED',
  decision_event: 'DECISION',
};

const DECISION_TONES = {
  EXEC: 'acid',
  FAIL: 'red',
  SKIP: 'amber',
  PASS: 'blue',
};

let sequence = 0;

/**
 * Project one raw websocket event into a display row:
 * { id, label, tone, text, timestamp }
 */
export function describeEvent(evt) {
  const raw = evt && typeof evt === 'object' ? evt : {};
  const data = raw.data && typeof raw.data === 'object' ? raw.data : {};
  const at = (key) => raw[key] ?? data[key];

  const type = raw.type ?? raw.event_type ?? 'event';
  const timestamp = Number(at('timestamp') ?? 0);
  const symbol = at('token_symbol') || at('symbol') || '';
  const name = at('token_name') || at('name') || '';
  const mint = at('token_mint') || at('mint') || '';
  const wallet = at('wallet') || at('creator_wallet') || at('target_wallet') || at('from_wallet') || '';
  const root = at('root_funder') || '';
  const kind = at('kind') || '';
  const reason = at('reason') || '';
  const amountLamports = at('amount_lamports') ?? at('funding_amount_lamports') ?? at('amount');

  const id =
    at('signature') ||
    at('created_signature') ||
    at('id') ||
    mint ||
    `${type}:${timestamp}:${++sequence}`;

  const baseLabel = EVENT_LABELS[type] ?? type.toUpperCase();

  let label = baseLabel;
  let tone = 'muted';
  let text = '';

  switch (type) {
    case 'launch_detected':
      tone = 'cyan';
      text = [
        symbol ? `$${symbol}` : '',
        name ? name : '',
        mint ? `mint ${shortId(mint)}` : '',
        wallet ? `dev ${shortId(wallet)}` : '',
      ]
        .filter(Boolean)
        .join(' · ');
      break;
    case 'decision_event':
      tone = DECISION_TONES[kind] ?? 'blue';
      label = kind ? `DECISION:${kind}` : 'DECISION';
      text = [symbol ? `$${symbol}` : '', reason ? reason : ''].filter(Boolean).join(' · ');
      break;
    case 'wallet_funded':
      tone = 'blue';
      text = [
        wallet ? `${shortId(wallet)} funded` : 'wallet funded',
        amountLamports != null ? `${formatSol(amountLamports)} SOL` : '',
        root ? `root ${shortId(root)}` : '',
      ]
        .filter(Boolean)
        .join(' · ');
      break;
    case 'transfer_detected':
      tone = 'muted';
      text = [
        amountLamports != null ? `${formatSol(amountLamports)} SOL` : 'transfer',
        wallet ? `→ ${shortId(wallet)}` : '',
        root ? `root ${shortId(root)}` : '',
      ]
        .filter(Boolean)
        .join(' · ');
      break;
    case 'funder_added':
      tone = 'acid';
      text = [
        wallet ? `${shortId(wallet)} registered as funder` : 'funder registered',
        root ? `root ${shortId(root)}` : '',
      ]
        .filter(Boolean)
        .join(' · ');
      break;
    case 'wallet_expired':
      tone = 'amber';
      text = wallet ? `${shortId(wallet)} expired` : 'wallet expired';
      break;
    case 'path_depth_limit_reached':
      tone = 'amber';
      text = wallet ? `depth limit at ${shortId(wallet)}` : 'depth limit reached';
      break;
    case 'path_stopped':
      tone = 'red';
      text = [wallet ? `chain stopped at ${shortId(wallet)}` : 'chain stopped', reason]
        .filter(Boolean)
        .join(' · ');
      break;
    default:
      tone = 'muted';
      text = [reason, wallet ? shortId(wallet) : ''].filter(Boolean).join(' · ') || 'no detail';
      break;
  }

  if (!text) text = 'no detail';

  return { id, label, tone, text, timestamp };
}