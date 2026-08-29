function resolveBase() {
  const configured = import.meta.env?.VITE_RUGBOT_API_BASE;
  const base = typeof configured === 'string' && configured.trim()
    ? configured.trim()
    : window.location.origin;
  return base.replace(/\/+$/, '');
}

export const API_BASE = resolveBase();
export const WS_URL = (() => {
  const url = new URL(API_BASE);
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  url.pathname = '/api/events';
  return url.toString();
})();

async function request(path, options = {}) {
  const init = { headers: { Accept: 'application/json' }, ...options };
  if (options.body) init.headers = { ...init.headers, 'Content-Type': 'application/json' };
  let response;
  try {
    response = await fetch(`${API_BASE}${path}`, init);
  } catch (error) {
    if (error?.name === 'AbortError') throw error;
    throw new Error(`backend unreachable at ${API_BASE}${path}`);
  }
  if (!response.ok) {
    let message = `backend error ${response.status} on ${path}`;
    try {
      const body = await response.json();
      message = body.detail || body.message || body.error || message;
    } catch {
      // The HTTP status is authoritative when the response has no JSON body.
    }
    throw new Error(message);
  }
  return response.json();
}

export function fetchState() {
  return request('/api/state');
}

export function fetchClusterTarget() {
  return request('/api/cluster/target');
}

export function fetchRecentTokens() {
  return request('/api/tokens/recent');
}

export function scanEntity(query, maxTransactions = 100, signal = undefined) {
  return request('/api/entity/scan', {
    method: 'POST',
    body: JSON.stringify({ query, max_transactions: maxTransactions }),
    signal,
  });
}

export function backtestEntity(targetAddress, quoteSizeSol = 0.10, slippagePct = 1.5) {
  return request('/api/entity/backtest', {
    method: 'POST',
    body: JSON.stringify({
      target_address: targetAddress,
      quote_size_sol: quoteSizeSol,
      slippage_pct: slippagePct,
    }),
  });
}

export function fetchCachedEntity(query) {
  return request(`/api/entity/cache?query=${encodeURIComponent(query)}`);
}

export function fetchEntityScanHistory(address, limit = 100) {
  return request(
    `/api/entity/${encodeURIComponent(address)}/scans?limit=${limit}`
  );
}

export function fetchWalletBalance(address) {
  return request(`/api/wallet/balance?address=${encodeURIComponent(address)}`);
}

export function trackEntity(address, label = 'Tracked entity') {
  return request('/api/entity/track', {
    method: 'POST',
    body: JSON.stringify({ address, label }),
  });
}
