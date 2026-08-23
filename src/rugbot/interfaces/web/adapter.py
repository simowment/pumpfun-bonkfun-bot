"""Aiohttp web bridge exposing the shared RugbotCore facade to a browser.

The adapter owns the HTTP/WebSocket transport only; all tracker and command
behavior lives in the shared ``RugbotCore`` and the command registry. The API
is UI-agnostic and does not duplicate tracker or command logic.
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from enum import Enum
from typing import TYPE_CHECKING

from aiohttp import web

from rugbot.application.commands import BotCommand, CommandResult
from rugbot.interfaces.base import BaseAdapter

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from rugbot.runtime.app import RugbotApp
    from rugbot.tracker.events import TrackerEvent


def jsonable(value: object) -> object:
    """Recursively convert a domain value into a JSON-safe structure.

    Dataclasses become mappings, enums become their values, and tuples and
    lists become lists. Nested values are converted recursively. Values that
    are already JSON-safe (str, int, float, bool, None) pass through unchanged.
    """
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {
            field.name: jsonable(getattr(value, field.name)) for field in fields(value)
        }
    if isinstance(value, tuple):
        return [jsonable(item) for item in value]
    if isinstance(value, list):
        return [jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    return value


class WebAdapter(BaseAdapter):
    """Bridge one RugbotCore to browser clients over HTTP and WebSocket.

    The adapter subscribes to core tracker events and broadcasts them to every
    connected WebSocket client. Inbound JSON command messages are validated and
    dispatched through the shared command registry.
    """

    def __init__(self, core: RugbotApp) -> None:
        """Store the core and initialize the empty client set.

        Args:
            core: The shared UI facade that owns all tracker and command logic.
        """
        self._core = core
        self._clients: set[web.WebSocketResponse] = set()
        self._subscribed = False

    async def connect(self) -> None:
        """Subscribe to every core tracker event."""
        self._core.subscribe(self._on_tracker_event)
        self._subscribed = True

    async def disconnect(self) -> None:
        """Unsubscribe from core events and close every connected client."""
        if self._subscribed:
            self._core.event_bus.unsubscribe("*", self._on_tracker_event)
            self._subscribed = False
        for ws in list(self._clients):
            await ws.close()
        self._clients.clear()

    async def send(self, event: TrackerEvent) -> None:
        """Broadcast one tracker event to every connected WebSocket client.

        Disconnected clients are dropped without raising, so a stale socket
        never crashes the shared event bus.
        """
        payload = {"type": "event", "data": jsonable(event)}
        for ws in list(self._clients):
            if ws.closed:
                self._clients.discard(ws)
                continue
            try:
                await ws.send_json(payload)
            except (ConnectionResetError, RuntimeError):
                self._clients.discard(ws)

    async def on_message(self, message: object) -> CommandResult | None:
        """Validate an inbound JSON-like command and dispatch it to the core.

        Returns the command result, or ``None`` when the message is malformed
        (missing or non-string ``name``, or non-list ``args``).
        """
        if not isinstance(message, dict):
            return None
        name = message.get("name")
        args = message.get("args", [])
        if not isinstance(name, str) or not name:
            return None
        if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
            return None
        command = BotCommand(name=name, args=tuple(args), source="web")
        return await self._core.execute_command(command)

    def register_client(self, ws: web.WebSocketResponse) -> None:
        """Track one connected WebSocket client for event broadcast."""
        self._clients.add(ws)

    def unregister_client(self, ws: web.WebSocketResponse) -> None:
        """Drop one WebSocket client from the broadcast set."""
        self._clients.discard(ws)

    async def _on_tracker_event(self, event: TrackerEvent) -> None:
        """Bridge a core tracker event to every connected WebSocket client."""
        await self.send(event)


def _state_projection(core: RugbotApp) -> dict[str, object]:
    """Project the core's current state into a JSON-safe mapping."""
    return {
        "targets": jsonable(core.targets()),
        "funders": jsonable(core.funders()),
        "wallets": jsonable(core.wallets()),
        "launches": jsonable(core.launches()),
        "positions": jsonable(core.positions()),
        "screener": jsonable(core.screener.get_candidates()),
        "snapshot": jsonable(core.snapshot()),
    }


def _json_response(payload: object, *, status: int = 200) -> web.Response:
    """Build a JSON response from an already JSON-safe payload."""
    return web.json_response(jsonable(payload), status=status)


@web.middleware
async def _cors_middleware(
    request: web.Request,
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> web.StreamResponse:
    """Add permissive CORS headers and answer OPTIONS preflight requests."""
    if request.method == "OPTIONS":
        response = web.Response(status=204)
    else:
        response = await handler(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


async def _handle_health(_request: web.Request) -> web.Response:
    """Return a lightweight liveness payload."""
    return _json_response({"status": "ok", "service": "rugbot-web"})


async def _handle_state(request: web.Request) -> web.Response:
    """Return the current JSON-safe state projection."""
    core = request.app["rugbot_core"]
    return _json_response(_state_projection(core))


async def _handle_get_screener(request: web.Request) -> web.Response:
    """Return discovered candidates in the review queue."""
    core: RugbotApp = request.app["rugbot_core"]
    candidates = core.screener.get_candidates()
    return _json_response(candidates)


async def _handle_accept_screener(request: web.Request) -> web.Response:
    """Accept and enroll a discovered developer into active tracking with optimal policy."""
    core: RugbotApp = request.app["rugbot_core"]
    try:
        data = await request.json()
    except (ValueError, TypeError):
        return _json_response({"ok": False, "error": "Invalid JSON body"}, status=400)

    address = data.get("address", "").strip()
    if not address:
        return _json_response({"ok": False, "error": "Address is required"}, status=400)

    candidate = core.screener.accept_candidate(address, core.service)
    if candidate is None:
        # If not already in screener, scan and accept directly
        candidate = core.screener.scan_and_evaluate(address)
        core.screener.accept_candidate(candidate.creator_wallet, core.service)

    return _json_response(
        {
            "ok": True,
            "message": f"Enrolled {candidate.creator_wallet[:8]}... as Tracked Target with TP {candidate.optimal_tp_label}",
            "candidate": candidate,
        }
    )


async def _handle_reject_screener(request: web.Request) -> web.Response:
    """Reject/dismiss a discovered developer candidate."""
    core: RugbotApp = request.app["rugbot_core"]
    try:
        data = await request.json()
    except (ValueError, TypeError):
        return _json_response({"ok": False, "error": "Invalid JSON body"}, status=400)

    address = data.get("address", "").strip()
    candidate = core.screener.reject_candidate(address)
    return _json_response(
        {"ok": True, "address": address, "rejected": candidate is not None}
    )


async def _handle_scan_screener(request: web.Request) -> web.Response:
    """Scan and evaluate a token mint or developer wallet against the Memecoin Bible."""
    core: RugbotApp = request.app["rugbot_core"]
    try:
        data = await request.json()
    except (ValueError, TypeError):
        return _json_response({"ok": False, "error": "Invalid JSON body"}, status=400)

    query = data.get("query", "").strip()
    if not query:
        return _json_response(
            {"ok": False, "error": "Query address is required"}, status=400
        )

    try:
        candidate = core.screener.scan_and_evaluate(query)
        return _json_response({"ok": True, "candidate": candidate})
    except (ValueError, KeyError, RuntimeError) as e:
        return _json_response({"ok": False, "error": str(e)}, status=500)


async def _handle_command(request: web.Request) -> web.Response:
    """Validate and dispatch one JSON command body to the core."""
    try:
        payload = await request.json()
    except ValueError:
        return _json_response(
            {"ok": False, "message": "request body must be valid JSON"},
            status=400,
        )
    adapter = request.app["rugbot_adapter"]
    result = await adapter.on_message(payload)
    if result is None:
        return _json_response(
            {
                "ok": False,
                "message": (
                    "malformed command: name must be a string and args must be "
                    "a list of strings"
                ),
            },
            status=400,
        )
    return _json_response(
        {"ok": result.ok, "message": result.message, "data": jsonable(result.data)}
    )


async def _handle_events(request: web.Request) -> web.WebSocketResponse:
    """Serve the live event stream over a WebSocket connection."""
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    adapter = request.app["rugbot_adapter"]
    core = request.app["rugbot_core"]
    await ws.send_json({"type": "state", "data": _state_projection(core)})
    adapter.register_client(ws)
    try:
        async for _message in ws:
            pass
    finally:
        adapter.unregister_client(ws)
    return ws


async def _handle_index(_request: web.Request) -> web.Response:
    """Serve the responsive, dark-themed GMGN/Axiom-style Web Trading Dashboard."""
    html_content = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Rugbot // Live Screener & B0 Sniper</title>
  <style>
    :root {
      --bg: #07090e;
      --card-bg: #0e121a;
      --border: #1a2233;
      --primary: #00f2fe;
      --primary-dim: rgba(0, 242, 254, 0.15);
      --success: #00e676;
      --success-dim: rgba(0, 230, 118, 0.15);
      --warning: #ffb300;
      --warning-dim: rgba(255, 179, 0, 0.15);
      --danger: #ff3d71;
      --danger-dim: rgba(255, 61, 113, 0.15);
      --text: #e2e8f0;
      --muted: #64748b;
      --font: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, monospace;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background-color: var(--bg);
      color: var(--text);
      font-family: var(--font);
      font-size: 13px;
      line-height: 1.4;
      padding: 16px;
    }
    header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding-bottom: 14px;
      border-bottom: 1px solid var(--border);
      margin-bottom: 16px;
      flex-wrap: wrap;
      gap: 12px;
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 10px;
    }
    .brand h1 {
      font-size: 18px;
      font-weight: 800;
      letter-spacing: 0.5px;
      background: linear-gradient(90deg, #00f2fe, #4facfe);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
    }
    .status-dot {
      width: 8px;
      height: 8px;
      background: var(--success);
      border-radius: 50%;
      box-shadow: 0 0 10px var(--success);
      animation: pulse 2s infinite;
    }
    @keyframes pulse {
      0%, 100% { opacity: 1; transform: scale(1); }
      50% { opacity: 0.4; transform: scale(0.85); }
    }
    .search-bar {
      display: flex;
      gap: 8px;
      flex: 1;
      max-width: 540px;
    }
    input[type="text"] {
      background: var(--card-bg);
      border: 1px solid var(--border);
      color: var(--text);
      padding: 8px 12px;
      border-radius: 6px;
      font-size: 13px;
      font-family: monospace;
      flex: 1;
      outline: none;
      transition: border 0.2s;
    }
    input[type="text"]:focus {
      border-color: var(--primary);
    }
    button {
      background: var(--primary);
      color: #07090e;
      border: none;
      padding: 8px 16px;
      border-radius: 6px;
      font-size: 12px;
      font-weight: 700;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      transition: all 0.2s;
    }
    button:hover { opacity: 0.9; transform: translateY(-1px); }
    button.btn-success { background: var(--success); color: #000; }
    button.btn-danger { background: var(--danger); color: #fff; }
    button.btn-outline {
      background: transparent;
      border: 1px solid var(--border);
      color: var(--text);
    }
    button.btn-outline:hover {
      border-color: var(--primary);
      color: var(--primary);
    }
    .stat-pills {
      display: flex;
      gap: 8px;
    }
    .stat-pill {
      background: var(--card-bg);
      border: 1px solid var(--border);
      padding: 6px 12px;
      border-radius: 6px;
      font-size: 12px;
      display: flex;
      gap: 6px;
      align-items: center;
    }
    .stat-pill .val { font-weight: 700; color: var(--primary); }
    
    .grid {
      display: grid;
      grid-template-columns: 1fr;
      gap: 16px;
      margin-bottom: 16px;
    }
    .card {
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 14px;
      overflow: hidden;
    }
    .card-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 12px;
      border-bottom: 1px solid var(--border);
      padding-bottom: 8px;
    }
    .card-title {
      font-size: 14px;
      font-weight: 700;
      color: var(--text);
      display: flex;
      align-items: center;
      gap: 8px;
    }
    
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 12px;
      text-align: left;
    }
    th {
      color: var(--muted);
      font-weight: 600;
      padding: 8px 10px;
      border-bottom: 1px solid var(--border);
      text-transform: uppercase;
      font-size: 11px;
    }
    td {
      padding: 10px;
      border-bottom: 1px solid var(--border);
      font-family: monospace;
    }
    tr:hover td {
      background: rgba(255, 255, 255, 0.02);
    }
    
    .badge {
      display: inline-block;
      padding: 3px 8px;
      border-radius: 4px;
      font-weight: 700;
      font-size: 10px;
      text-transform: uppercase;
    }
    .badge-qualified { background: var(--success-dim); color: var(--success); border: 1px solid var(--success); }
    .badge-pending { background: var(--warning-dim); color: var(--warning); border: 1px solid var(--warning); }
    .badge-accepted { background: var(--primary-dim); color: var(--primary); border: 1px solid var(--primary); }
    .badge-rejected { background: var(--danger-dim); color: var(--danger); border: 1px solid var(--danger); }
    
    .addr-box {
      display: inline-flex;
      align-items: center;
      gap: 4px;
    }
    .addr-link {
      color: var(--primary);
      text-decoration: none;
    }
    .addr-link:hover { text-decoration: underline; }
    .copy-btn {
      background: none;
      border: none;
      color: var(--muted);
      cursor: pointer;
      padding: 2px 4px;
      font-size: 11px;
    }
    .copy-btn:hover { color: var(--primary); }
    
    .toast {
      position: fixed;
      bottom: 20px;
      right: 20px;
      background: var(--card-bg);
      border: 1px solid var(--primary);
      padding: 12px 20px;
      border-radius: 6px;
      box-shadow: 0 4px 20px rgba(0, 0, 0, 0.5);
      transform: translateY(100px);
      opacity: 0;
      transition: all 0.3s cubic-bezier(0.18, 0.89, 0.32, 1.28);
      z-index: 999;
      font-weight: 600;
      color: var(--primary);
    }
    .toast.show { transform: translateY(0); opacity: 1; }
  </style>
</head>
<body>

  <header>
    <div class="brand">
      <div class="status-dot" id="ws-status"></div>
      <h1>RUGBOT // LIVE SCREENER & REVIEW</h1>
    </div>

    <div class="search-bar">
      <input type="text" id="scan-input" placeholder="Paste Token Mint or Dev Wallet (e.g. E9CqsGL5...)" />
      <button onclick="scanAddress()">🔍 SCAN & AUDIT</button>
    </div>

    <div class="stat-pills">
      <div class="stat-pill">Targets: <span class="val" id="count-targets">0</span></div>
      <div class="stat-pill">Review Queue: <span class="val" id="count-candidates">0</span></div>
      <div class="stat-pill">Mode: <span class="val" style="color:var(--success)">SIMULATED B0</span></div>
    </div>
  </header>

  <div class="grid">
    <!-- SECTION 1: CANDIDATES REVIEW QUEUE -->
    <div class="card">
      <div class="card-header">
        <div class="card-title">
          <span>⚡ CANDIDATE DEVELOPER REVIEW QUEUE (Pump.fun Live Stream & Scans)</span>
        </div>
        <button class="btn-outline" onclick="loadState()">🔄 Refresh</button>
      </div>

      <div style="overflow-x:auto;">
        <table>
          <thead>
            <tr>
              <th>Status</th>
              <th>Token</th>
              <th>Creator Dev</th>
              <th>Master Funder</th>
              <th>Cluster Launches</th>
              <th>Winrate</th>
              <th>Optimal TP</th>
              <th>Expected Net EV</th>
              <th>Bible Qualification</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody id="screener-table-body">
            <tr><td colspan="10" style="text-align:center; color:var(--muted); padding:20px;">No candidates in queue. Scan a token above or wait for stream events.</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- SECTION 2: ACTIVE WATCHLIST -->
    <div class="card">
      <div class="card-header">
        <div class="card-title">
          <span>🎯 ACTIVE TRACKED TARGETS & ARMED SNIPER POLICIES</span>
        </div>
      </div>

      <div style="overflow-x:auto;">
        <table>
          <thead>
            <tr>
              <th>Target Address / Funder</th>
              <th>Label</th>
              <th>Mode</th>
              <th>Size</th>
              <th>Take Profit</th>
              <th>Stop Loss</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody id="targets-table-body">
            <tr><td colspan="7" style="text-align:center; color:var(--muted); padding:20px;">No active targets enrolled.</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>

  <div id="toast" class="toast">Action completed</div>

  <script>
    function shortAddr(a) {
      if (!a) return '—';
      return a.slice(0, 4) + '...' + a.slice(-4);
    }

    function copyToClipboard(text) {
      navigator.clipboard.writeText(text).then(() => {
        showToast('📋 Copied: ' + text);
      });
    }

    function showToast(msg) {
      const t = document.getElementById('toast');
      t.innerText = msg;
      t.classList.add('show');
      setTimeout(() => t.classList.remove('show'), 2500);
    }

    async function scanAddress() {
      const q = document.getElementById('scan-input').value.trim();
      if (!q) return;
      showToast('🔍 Analyzing on-chain cluster for ' + shortAddr(q) + '...');
      try {
        const res = await fetch('/api/screener/scan', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ query: q })
        });
        const data = await res.json();
        if (data.ok) {
          showToast('✅ Candidate added to review queue: ' + data.candidate.token_symbol);
          loadState();
        } else {
          showToast('❌ Scan failed: ' + (data.error || data.message));
        }
      } catch (err) {
        showToast('❌ Error: ' + err.message);
      }
    }

    async function acceptCandidate(addr) {
      showToast('⚡ Enrolling target: ' + shortAddr(addr) + '...');
      try {
        const res = await fetch('/api/screener/accept', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ address: addr })
        });
        const data = await res.json();
        if (data.ok) {
          showToast('🎯 ' + data.message);
          loadState();
        } else {
          showToast('❌ Failed: ' + data.error);
        }
      } catch (err) {
        showToast('❌ Error: ' + err.message);
      }
    }

    async function rejectCandidate(addr) {
      try {
        await fetch('/api/screener/reject', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ address: addr })
        });
        showToast('Dismissed candidate ' + shortAddr(addr));
        loadState();
      } catch (err) {
        showToast('❌ Error: ' + err.message);
      }
    }

    async function loadState() {
      try {
        const res = await fetch('/api/state');
        const state = await res.json();
        renderState(state);
      } catch (err) {
        console.error('State load error:', err);
      }
    }

    function renderState(state) {
      const candidates = state.screener || [];
      const targets = state.targets || [];
      
      document.getElementById('count-targets').innerText = targets.length;
      document.getElementById('count-candidates').innerText = candidates.length;

      // Render Screener Table
      const sBody = document.getElementById('screener-table-body');
      if (candidates.length === 0) {
        sBody.innerHTML = '<tr><td colspan="10" style="text-align:center; color:var(--muted); padding:20px;">No candidates in queue. Scan a token above or wait for stream events.</td></tr>';
      } else {
        sBody.innerHTML = candidates.map(c => {
          let badgeClass = 'badge-pending';
          if (c.status === 'QUALIFIED') badgeClass = 'badge-qualified';
          if (c.status === 'ACCEPTED') badgeClass = 'badge-accepted';
          if (c.status === 'REJECTED') badgeClass = 'badge-rejected';

          const devShort = shortAddr(c.creator_wallet);
          const funderShort = shortAddr(c.root_funder);

          return `<tr>
            <td><span class="badge ${badgeClass}">${c.status}</span></td>
            <td>
              <strong>${c.token_symbol}</strong> <span style="color:var(--muted)">(${c.token_name})</span>
              <a href="https://dexscreener.com/solana/${c.token_mint}" target="_blank" style="color:var(--primary); margin-left:4px; text-decoration:none;">↗</a>
            </td>
            <td>
              <span class="addr-box">
                <a class="addr-link" href="https://solscan.io/account/${c.creator_wallet}" target="_blank">${devShort}</a>
                <button class="copy-btn" onclick="copyToClipboard('${c.creator_wallet}')">📋</button>
              </span>
            </td>
            <td>
              <span class="addr-box">
                <a class="addr-link" href="https://solscan.io/account/${c.root_funder}" target="_blank">${funderShort}</a>
                <button class="copy-btn" onclick="copyToClipboard('${c.root_funder}')">📋</button>
              </span>
            </td>
            <td><strong>${c.cluster_token_count}</strong> tokens</td>
            <td style="color:${c.winrate_pct >= 33 ? 'var(--success)' : 'var(--warning)'}; font-weight:700;">${c.winrate_pct.toFixed(0)}%</td>
            <td style="color:var(--primary); font-weight:700;">${c.optimal_tp_label}</td>
            <td style="color:${c.optimal_net_ev_sol > 0 ? 'var(--success)' : 'var(--danger)'}; font-weight:700;">${c.optimal_net_ev_sol > 0 ? '+' : ''}${c.optimal_net_ev_sol.toFixed(4)} SOL</td>
            <td style="font-size:11px; color:var(--muted);">${c.qualification_reason || 'Evaluated'}</td>
            <td>
              ${c.status !== 'ACCEPTED' ? `
                <button class="btn-success" style="padding:4px 8px; font-size:11px;" onclick="acceptCandidate('${c.creator_wallet}')">🎯 ENROLL</button>
                <button class="btn-outline" style="padding:4px 8px; font-size:11px; margin-left:4px;" onclick="rejectCandidate('${c.creator_wallet}')">✕</button>
              ` : '<span style="color:var(--primary); font-weight:700;">✓ ACTIVE</span>'}
            </td>
          </tr>`;
        }).join('');
      }

      // Render Targets Table
      const tBody = document.getElementById('targets-table-body');
      if (targets.length === 0) {
        tBody.innerHTML = '<tr><td colspan="7" style="text-align:center; color:var(--muted); padding:20px;">No active targets enrolled.</td></tr>';
      } else {
        tBody.innerHTML = targets.map(([funder, policy]) => {
          const tpPct = policy ? (policy.take_profit_pnl_ppm / 10000).toFixed(0) + '%' : '100%';
          const slPct = policy ? (policy.stop_loss_pnl_ppm / 10000).toFixed(0) + '%' : '-30%';
          const sizeSol = policy ? (policy.quote_size_lamports / 1000000000).toFixed(3) + ' SOL' : '0.025 SOL';
          const mode = policy ? policy.execution_mode : 'SIMULATED';

          return `<tr>
            <td>
              <span class="addr-box">
                <a class="addr-link" href="https://solscan.io/account/${funder.address}" target="_blank">${shortAddr(funder.address)}</a>
                <button class="copy-btn" onclick="copyToClipboard('${funder.address}')">📋</button>
              </span>
            </td>
            <td>${funder.label || 'Dev Target'}</td>
            <td><span class="badge badge-accepted">${mode}</span></td>
            <td>${sizeSol}</td>
            <td style="color:var(--success); font-weight:700;">${tpPct}</td>
            <td style="color:var(--danger); font-weight:700;">${slPct}</td>
            <td><span style="color:var(--success); font-weight:700;">● ARMED</span></td>
          </tr>`;
        }).join('');
      }
    }

    // Connect WebSocket
    function connectWS() {
      const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
      const ws = new WebSocket(proto + '//' + window.location.host + '/api/events');
      ws.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data);
          if (msg.type === 'state') {
            renderState(msg.data);
          } else {
            loadState();
          }
        } catch (e) {}
      };
      ws.onclose = () => {
        setTimeout(connectWS, 3000);
      };
    }

    document.getElementById('scan-input').addEventListener('keypress', (e) => {
      if (e.key === 'Enter') scanAddress();
    });

    loadState();
    connectWS();
  </script>
</body>
</html>
"""
    return web.Response(text=html_content, content_type="text/html")


async def _on_startup(app: web.Application) -> None:
    """Subscribe the adapter and start the shared observation runtime."""
    adapter = app["rugbot_adapter"]
    core: RugbotApp = app["rugbot_core"]
    await adapter.connect()
    await core.start()


async def _on_cleanup(app: web.Application) -> None:
    """Unsubscribe the adapter and release the shared core."""
    adapter = app["rugbot_adapter"]
    await adapter.disconnect()
    core = app["rugbot_core"]
    await core.close()


def create_web_app(core: RugbotApp) -> web.Application:
    """Build an aiohttp application exposing the RugbotApp web bridge and Screener Dashboard.

    Args:
        core: The shared RugbotApp facade to expose over HTTP and WebSocket.

    Returns:
        A configured ``aiohttp.web.Application`` with the API and UI routes wired up.
    """
    adapter = WebAdapter(core)
    app = web.Application(middlewares=[_cors_middleware])
    app["rugbot_core"] = core
    app["rugbot_adapter"] = adapter

    # Dashboard UI
    app.router.add_get("/", _handle_index)

    # REST APIs
    app.router.add_get("/api/health", _handle_health)
    app.router.add_get("/api/state", _handle_state)
    app.router.add_get("/api/screener", _handle_get_screener)
    app.router.add_post("/api/screener/accept", _handle_accept_screener)
    app.router.add_post("/api/screener/reject", _handle_reject_screener)
    app.router.add_post("/api/screener/scan", _handle_scan_screener)
    app.router.add_post("/api/command", _handle_command)

    # Real-time WebSocket
    app.router.add_get("/api/events", _handle_events)

    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    return app


__all__ = ["WebAdapter", "create_web_app", "jsonable"]
