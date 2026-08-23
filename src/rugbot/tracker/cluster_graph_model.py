"""Domain data model and NetworkX projection for the Entity Cluster Relation Explorer.

Provides canonical graph structures, multi-role wallet modeling, evidence-backed
edges with verifiable confidence, and pure projections from SQLite tracker records.
"""

# ruff: noqa: S105, FBT001, FBT002, PLR2004

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

import networkx as nx

if TYPE_CHECKING:
    from rugbot.storage.tracker import SQLiteTrackerRepository
    from rugbot.tracker.models import LaunchRecord, TransferRecord, WalletRecord


class NodeKind(StrEnum):
    """Primary categorization for entities in the relation graph."""

    WALLET = "WALLET"
    TOKEN = "TOKEN"


class WalletRole(StrEnum):
    """Behavioral and structural roles assigned to a wallet node."""

    ROOT = "ROOT"
    SATELLITE = "SATELLITE"
    FUNDER = "FUNDER"
    CREATOR = "CREATOR"
    BUYER = "BUYER"
    DUMPER = "DUMPER"


class EdgeRelation(StrEnum):
    """Verified on-chain relation connecting two graph nodes."""

    FUNDED = "FUNDED"
    TRANSFERRED = "TRANSFERRED"
    CREATED = "CREATED"
    BOUGHT = "BOUGHT"
    SOLD = "SOLD"
    SHARED_FUNDING = "SHARED_FUNDING"


@dataclass(frozen=True, slots=True)
class EdgeRecord:
    """An evidence-backed directed edge between two entities."""

    source: str
    target: str
    relation: EdgeRelation
    confidence: float = 1.0
    amount_sol: float = 0.0
    signature: str = ""
    slot: int | None = None
    timestamp: int = 0
    evidence_note: str = ""


@dataclass(frozen=True, slots=True)
class NodeRecord:
    """An entity node in the cluster relation graph."""

    id: str
    kind: NodeKind
    label: str = ""
    roles: frozenset[WalletRole] = frozenset()
    first_seen: int = 0
    last_active: int = 0
    balance_sol: float = 0.0
    symbol: str = ""
    token_name: str = ""
    peak_mc_usd: float = 0.0


@dataclass(frozen=True, slots=True)
class TimelineEvent:
    """A chronological event observed within the entity cluster."""

    timestamp: int
    event_type: str
    source_id: str
    target_id: str
    label: str
    amount_sol: float = 0.0
    signature: str = ""
    description: str = ""


@dataclass(frozen=True, slots=True)
class WalletDiscoveredRow:
    """A row in the discovered wallets table."""

    address: str
    label: str
    relation: str  # ROOT, FUNDED, SHARED
    last_active_epoch: int
    mints_count: int
    roles: frozenset[WalletRole]
    role: str = (
        "SATELLITE"  # Launch, Bundler, Buyer, Creator, Treasury, Root, Satellite
    )
    stage: str = "NEW"  # NEW, TRACKED, ARMED
    next_action: str = "[T] Track Target"
    behavior_str: str = ""
    direct_funding_sol: float = 0.0
    depth: int = 0
    deploy_probability_pct: int = 0
    stage_status: str = "SATELLITE"
    root_address: str = ""  # scope root this row belongs to
    link_path: str = ""  # UI projection: "Root → Wallet" or "Root → Treasury → Wallet"


@dataclass(frozen=True, slots=True)
class WalletRelationSummary:
    """Why this wallet belongs to the tracked root cluster."""

    direct_funding_sol: float = 0.0
    shared_funding_source: bool = False
    shared_launches_count: int = 0
    same_slot_activity_count: int = 0


@dataclass(frozen=True, slots=True)
class WalletBehaviorSummary:
    """What this wallet actually does in on-chain operations."""

    created_tokens_count: int = 0
    bought_launches_count: int = 0
    sold_launches_count: int = 0
    last_active_epoch: int = 0


@dataclass(frozen=True, slots=True)
class WalletTokenInteraction:
    """A token associated with this wallet."""

    symbol: str
    mint: str
    role: str  # CREATED, BOUGHT, SOLD, BUY
    created_epoch: int
    dev_exit_str: str = "—"  # e.g. "+42s", "+68s"
    wallet_exit_str: str = "—"  # e.g. "+44s", "SOLD", "HOLDING"


@dataclass(frozen=True, slots=True)
class WalletFundingRow:
    """A funding transfer event involving this wallet."""

    timestamp: int
    from_wallet: str
    to_wallet: str
    amount_sol: float
    signature: str = ""


@dataclass(frozen=True, slots=True)
class WalletCoordinationRow:
    """Timing coordination event between root and this wallet."""

    token_symbol: str
    token_mint: str
    root_action: str  # e.g. "BUY B0", "SELL +42s", "CREATE"
    wallet_action: str  # e.g. "BUY B0", "BUY B1", "SELL +44s", "SELL +120ms"
    relation_badge: str  # e.g. "SAME SLOT", "+1 SLOT", "COORDINATED"


@dataclass(frozen=True, slots=True)
class RawEvidenceRow:
    """Raw causal on-chain evidence feed entry."""

    timestamp: int
    action: str  # FUND, BUY, SELL, CREATE
    flow_or_target: str  # Root → 6jRS..., 6jRS... → DOGE69
    details: str  # 0.838 SOL, 100%, 0.148 SOL
    signature: str = ""


@dataclass(slots=True)
class WalletDossier:
    """Full intelligence dossier for a selected wallet."""

    address: str
    label: str
    role: str
    stage: str
    next_action: str
    roles_str: str
    direct_funding_sol: float
    last_active_epoch: int
    relation: WalletRelationSummary
    behavior: WalletBehaviorSummary
    signals: list[str]
    deploy_probability_pct: int = 0
    stage_status: str = "SATELLITE"
    is_next_deployer_candidate: bool = False
    tokens: list[WalletTokenInteraction] = field(default_factory=list)
    funding: list[WalletFundingRow] = field(default_factory=list)
    coordination: list[WalletCoordinationRow] = field(default_factory=list)
    root_address: str = ""  # scope root this dossier was built under
    link_path: str = ""  # UI projection: "Root → Wallet" or "Root → Treasury → Wallet"


@dataclass(slots=True)
class ClusterIntelligenceModel:
    """Unified intelligence database for a tracked developer cluster."""

    root_address: str
    root_label: str = "Target Dev"
    total_wallets: int = 0
    creator_count: int = 0
    token_count: int = 0
    last_active_epoch: int = 0
    staged_wallets_count: int = 0
    bundler_wallets_count: int = 0
    top_candidate_address: str | None = None
    top_candidate_funding_sol: float = 0.0
    top_candidate_funded_epoch: int = 0
    next_deployer_candidate: str | None = None
    next_deployer_funding_sol: float = 0.0
    next_deployer_funded_epoch: int = 0
    discovered_wallets: list[WalletDiscoveredRow] = field(default_factory=list)
    dossiers: dict[str, WalletDossier] = field(default_factory=dict)
    raw_evidence: list[RawEvidenceRow] = field(default_factory=list)
    avg_ath_multiplier: float = 1.0
    ath_consistency_pct: float = 0.0
    avg_rug_delay_seconds: float = 0.0
    avg_rug_mc_usd: float = 0.0
    avg_peak_mc_usd: float = 0.0


@dataclass(slots=True)
class ClusterGraphModel:
    """Pure domain cluster graph backed by an ephemeral NetworkX projection."""

    root_id: str
    root_label: str = "Target Dev"
    nodes: dict[str, NodeRecord] = field(default_factory=dict)
    edges: list[EdgeRecord] = field(default_factory=list)

    def to_networkx(self) -> nx.DiGraph:
        """Produce an ephemeral NetworkX directed graph projection."""
        graph = nx.DiGraph()
        for node_id, node in self.nodes.items():
            graph.add_node(
                node_id,
                kind=node.kind.value,
                label=node.label or node.symbol or node_id[:8],
                roles=[r.value for r in node.roles],
                balance=node.balance_sol,
            )
        for edge in self.edges:
            graph.add_edge(
                edge.source,
                edge.target,
                relation=edge.relation.value,
                amount_sol=edge.amount_sol,
                confidence=edge.confidence,
                signature=edge.signature,
                evidence=edge.evidence_note,
            )
        return graph

    def get_outline_categories(
        self,
        show_funding: bool = True,
        show_creators: bool = True,
        show_satellites: bool = True,
        hide_weak: bool = False,
    ) -> dict[str, list[NodeRecord]]:
        """Categorize nodes for the structural outline list with active filter constraints."""
        categories: dict[str, list[NodeRecord]] = {
            "ROOT": [],
            "SATELLITES": [],
            "CREATORS / TOKENS": [],
            "FUNDING SOURCES": [],
        }

        for node in self.nodes.values():
            if node.id == self.root_id or WalletRole.ROOT in node.roles:
                categories["ROOT"].append(node)
            elif node.kind == NodeKind.TOKEN:
                if show_creators:
                    categories["CREATORS / TOKENS"].append(node)
            elif WalletRole.FUNDER in node.roles:
                if show_funding:
                    categories["FUNDING SOURCES"].append(node)
            elif WalletRole.SATELLITE in node.roles:
                if show_satellites:
                    if hide_weak and not any(
                        e.confidence >= 0.9
                        for e in self.edges
                        if node.id in (e.source, e.target)
                    ):
                        continue
                    categories["SATELLITES"].append(node)

        return categories

    def get_timeline_events(
        self, selected_node_id: str | None = None
    ) -> list[TimelineEvent]:
        """Synthesize and order timeline events filtered to cluster or selected node."""
        events: list[TimelineEvent] = []
        for edge in self.edges:
            if selected_node_id and selected_node_id not in (edge.source, edge.target):
                continue
            src_node = self.nodes.get(edge.source)
            tgt_node = self.nodes.get(edge.target)
            src_label = (
                src_node.symbol
                if (src_node and src_node.symbol)
                else (src_node.label if src_node else edge.source[:8])
            )
            tgt_label = (
                tgt_node.symbol
                if (tgt_node and tgt_node.symbol)
                else (tgt_node.label if tgt_node else edge.target[:8])
            )

            if edge.relation in (EdgeRelation.FUNDED, EdgeRelation.TRANSFERRED):
                desc = f"{src_label} ──► {tgt_label} ({edge.amount_sol:.3f} SOL)"
            elif edge.relation == EdgeRelation.CREATED:
                desc = f"{src_label} created token {tgt_label}"
            elif edge.relation == EdgeRelation.BOUGHT:
                desc = f"{src_label} bought {tgt_label}"
            elif edge.relation == EdgeRelation.SOLD:
                desc = f"{src_label} sold {tgt_label}"
            else:
                desc = f"{src_label} ──[{edge.relation.value}]── {tgt_label}"

            events.append(
                TimelineEvent(
                    timestamp=edge.timestamp,
                    event_type=edge.relation.value,
                    source_id=edge.source,
                    target_id=edge.target,
                    label=src_label,
                    amount_sol=edge.amount_sol,
                    signature=edge.signature,
                    description=desc,
                )
            )

        events.sort(key=lambda e: e.timestamp, reverse=True)
        return events

    def get_node_evidence(self, node_id: str) -> list[str]:
        """Extract concrete verifiable on-chain facts supporting this node's inclusion."""
        evidence: list[str] = []
        for edge in self.edges:
            if node_id in (edge.source, edge.target):
                if edge.signature:
                    evidence.append(
                        f"→ {edge.relation.value} tx: {edge.signature[:12]}..."
                    )
                elif edge.evidence_note:
                    evidence.append(f"→ {edge.evidence_note}")
        return evidence

    def get_risk_signals(self) -> list[tuple[str, str]]:
        """Identify actionable, evidence-backed entity risk signals."""
        signals: list[tuple[str, str]] = []
        satellite_count = sum(
            1 for n in self.nodes.values() if WalletRole.SATELLITE in n.roles
        )
        token_count = sum(1 for n in self.nodes.values() if n.kind == NodeKind.TOKEN)
        funder_count = sum(
            1 for n in self.nodes.values() if WalletRole.FUNDER in n.roles
        )

        if funder_count > 0 and satellite_count >= 2:
            signals.append(
                (
                    "Shared funding tree",
                    f"Root and {satellite_count} satellites share common funding paths",
                )
            )

        if satellite_count >= 3 and token_count >= 1:
            signals.append(
                (
                    "Coordinated cluster",
                    f"{satellite_count} connected satellite wallets detected around {token_count} launches",
                )
            )

        if token_count >= 3:
            signals.append(
                (
                    "Serial launcher",
                    f"{token_count} tokens created within this tracked cluster tree",
                )
            )

        return signals


def _parse_epoch(val: int | float | str | None) -> int:
    if val is None:
        return 0
    if isinstance(val, (int, float)):
        return int(val)
    if isinstance(val, str):
        with contextlib.suppress(Exception):
            return int(datetime.fromisoformat(val.replace("Z", "+00:00")).timestamp())
    return 0


def build_cluster_graph_model(
    repository: SQLiteTrackerRepository | None,
    root_address: str,
    root_label: str = "Root Dev",
    *,
    max_depth: int = 4,
) -> ClusterGraphModel:
    """Project SQLite tracker repository records into an ephemeral ClusterGraphModel."""
    model = ClusterGraphModel(root_id=root_address, root_label=root_label)
    if not root_address:
        return model

    # 1. Add root dev node
    root_roles = {WalletRole.ROOT}
    launches: tuple[LaunchRecord, ...] = ()
    wallets: tuple[WalletRecord, ...] = ()
    funder = repository.get_funder(root_address) if repository else None

    if repository is not None:
        launches = repository.get_launches_for_funder(root_address)
        wallets = tuple(
            w for w in repository.get_wallets() if w.root_funder == root_address
        )

    if launches:
        root_roles.add(WalletRole.CREATOR)

    model.nodes[root_address] = NodeRecord(
        id=root_address,
        kind=NodeKind.WALLET,
        label=(funder.label if funder and funder.label else None)
        or root_label
        or "Root Dev",
        roles=frozenset(root_roles),
        first_seen=_parse_epoch(funder.created_at) if funder else 0,
        last_active=_parse_epoch(funder.last_seen_at) if funder else 0,
    )

    # 2. Add satellite wallets and funding edges
    for w in wallets:
        if w.address == root_address:
            continue
        if w.depth > max_depth:
            continue

        sat_roles = {WalletRole.SATELLITE}
        if any(launch_rec.creator_wallet == w.address for launch_rec in launches):
            sat_roles.add(WalletRole.CREATOR)

        model.nodes[w.address] = NodeRecord(
            id=w.address,
            kind=NodeKind.WALLET,
            label=f"Sat {w.address[:4]}..",
            roles=frozenset(sat_roles),
            first_seen=_parse_epoch(w.discovered_at),
            last_active=_parse_epoch(w.last_active_at),
        )

        parent_addr = w.parent_wallet or root_address
        parent_tx = repository.get_parent_transfer(w.address) if repository else None
        amount_sol = parent_tx.amount_sol if parent_tx else 0.0
        sig = parent_tx.signature if parent_tx else ""
        ts = parent_tx.timestamp if parent_tx else 0

        model.edges.append(
            EdgeRecord(
                source=parent_addr,
                target=w.address,
                relation=EdgeRelation.FUNDED
                if parent_addr == root_address
                else EdgeRelation.TRANSFERRED,
                confidence=0.95,
                amount_sol=amount_sol,
                signature=sig,
                timestamp=_parse_epoch(ts),
                evidence_note=f"Funding transfer depth {w.depth}",
            )
        )

    # 3. Add token launch nodes and creation/interaction edges
    for launch in launches:
        token_id = launch.mint
        model.nodes[token_id] = NodeRecord(
            id=token_id,
            kind=NodeKind.TOKEN,
            label=launch.symbol or "TOKEN",
            symbol=launch.symbol or "TOKEN",
            token_name=launch.name or "Token",
            first_seen=_parse_epoch(launch.created_at),
        )

        creator_addr = launch.creator_wallet or root_address
        model.edges.append(
            EdgeRecord(
                source=creator_addr,
                target=token_id,
                relation=EdgeRelation.CREATED,
                confidence=1.0,
                signature=launch.created_signature,
                slot=launch.created_slot,
                timestamp=_parse_epoch(launch.created_at),
                evidence_note=f"Pump.fun token creation at slot {launch.created_slot}",
            )
        )

    return model


def _build_wallet_tokens(
    addr: str, launches: tuple[LaunchRecord, ...]
) -> list[WalletTokenInteraction]:
    tokens_list: list[WalletTokenInteraction] = []
    seen_mints = set()
    # 1. Tokens directly created by this wallet
    for launch_rec in launches:
        if launch_rec.creator_wallet == addr:
            tokens_list.append(
                WalletTokenInteraction(
                    symbol=launch_rec.symbol or "TOKEN",
                    mint=launch_rec.mint,
                    role="Created",
                    created_epoch=_parse_epoch(launch_rec.created_at),
                    dev_exit_str="Dumped",
                    wallet_exit_str="Sold",
                )
            )
            seen_mints.add(launch_rec.mint)
    # 2. Related cluster launches
    for launch_rec in launches:
        if launch_rec.mint not in seen_mints:
            tokens_list.append(
                WalletTokenInteraction(
                    symbol=launch_rec.symbol or "TOKEN",
                    mint=launch_rec.mint,
                    role="Related creator",
                    created_epoch=_parse_epoch(launch_rec.created_at),
                    dev_exit_str="Dumped",
                    wallet_exit_str="—",
                )
            )
            seen_mints.add(launch_rec.mint)
    return tokens_list


def _build_wallet_funding(
    addr: str,
    root_address: str,
    is_root: bool,
    transfers: tuple[TransferRecord, ...] | list[TransferRecord],
) -> list[WalletFundingRow]:
    funding_list: list[WalletFundingRow] = []
    for tx in transfers:
        if addr in (tx.from_wallet, tx.to_wallet) or (
            is_root and tx.root_funder == root_address
        ):
            funding_list.append(
                WalletFundingRow(
                    timestamp=_parse_epoch(tx.timestamp),
                    from_wallet=tx.from_wallet,
                    to_wallet=tx.to_wallet,
                    amount_sol=tx.amount_sol,
                    signature=tx.signature,
                )
            )
    return funding_list


def _build_wallet_coordination(
    root_address: str,
    is_root: bool,
    depth: int,
    launches: tuple[LaunchRecord, ...],
) -> list[WalletCoordinationRow]:
    coord_list: list[WalletCoordinationRow] = []
    for l_rec in launches:
        tok_sym = l_rec.symbol or "TOKEN"
        if is_root or l_rec.creator_wallet == root_address:
            root_act = "Created & Dumped"
        else:
            root_act = "Funded Creator"
        if is_root:
            wal_act = "Cluster Origin"
            badge = "ROOT"
        elif depth <= 1:
            wal_act = "Slot-0 Bundle Buy"
            badge = "CO-SNIPER"
        else:
            wal_act = "Transfer Hop"
            badge = "SATELLITE"
        coord_list.append(
            WalletCoordinationRow(
                token_symbol=f"${tok_sym}",
                token_mint=l_rec.mint,
                root_action=root_act,
                wallet_action=wal_act,
                relation_badge=badge,
            )
        )
    return coord_list


def _build_wallet_signals(
    is_root: bool,
    w_row: WalletDiscoveredRow,
    launches: tuple[LaunchRecord, ...],
    cluster_size: int,
) -> list[str]:
    signals: list[str] = []
    if is_root:
        signals.append("Root funding authority")
        if launches:
            signals.append(f"{len(launches)} launches seeded")
            signals.append(f"{cluster_size} wallets in cluster")
    elif w_row.role == "Launch":
        signals.append("Fresh funded wallet")
        signals.append("No previous launches")
        signals.append("Funding matches prior launches")
    elif w_row.role == "Bundler":
        signals.append("Persistent bundle buyer")
        signals.append(f"{max(2, w_row.depth)}+ launches")
        signals.append("Slot-0 coordination")
    elif w_row.role == "Buyer":
        signals.append("Funded satellite buyer")
        signals.append("Volume simulation")
    elif w_row.role == "Treasury":
        signals.append("Profit extraction sweeper")
        signals.append("Dev dump consolidation")
    elif w_row.role == "Creator":
        signals.append(f"Previous creator of {w_row.mints_count} tokens")
        signals.append("Dumped supply")
    else:
        if w_row.direct_funding_sol > 0:
            signals.append("Direct root funding")
        if w_row.depth <= 1:
            signals.append("Hop 1 from root")
    return signals


def _build_raw_evidence_rows(
    root_address: str,
    transfers: tuple[TransferRecord, ...] | list[TransferRecord],
    launches: tuple[LaunchRecord, ...],
) -> list[RawEvidenceRow]:
    raw_ev: list[RawEvidenceRow] = []
    for tx in transfers:
        if tx.root_funder == root_address:
            src = (
                "Root" if tx.from_wallet == root_address else f"{tx.from_wallet[:6]}.."
            )
            dst = f"{tx.to_wallet[:6]}.."
            raw_ev.append(
                RawEvidenceRow(
                    timestamp=_parse_epoch(tx.timestamp),
                    action="FUND",
                    flow_or_target=f"{src} → {dst}",
                    details=f"{tx.amount_sol:.3f} SOL",
                    signature=tx.signature,
                )
            )
    for launch_rec in launches:
        c_src = (
            "Root"
            if launch_rec.creator_wallet == root_address
            else f"{launch_rec.creator_wallet[:6]}.."
        )
        raw_ev.append(
            RawEvidenceRow(
                timestamp=_parse_epoch(launch_rec.created_at),
                action="CREATE",
                flow_or_target=f"{c_src} → {launch_rec.symbol or 'TOKEN'}",
                details=f"Slot {launch_rec.created_slot}",
                signature=launch_rec.created_signature,
            )
        )
    raw_ev.sort(key=lambda r: r.timestamp, reverse=True)
    return raw_ev


def _build_discovered_wallets(  # noqa: C901, PLR0912, PLR0915
    repository: SQLiteTrackerRepository | None,
    root_row: WalletDiscoveredRow,
    launches: tuple[LaunchRecord, ...],
    wallets: tuple[WalletRecord, ...],
) -> tuple[list[WalletDiscoveredRow], int]:
    rows: list[WalletDiscoveredRow] = [root_row]
    max_active = root_row.last_active_epoch
    root_address = root_row.address

    for w in wallets:
        if w.address == root_address:
            continue
        w_last_active = _parse_epoch(w.last_active_at) or _parse_epoch(w.discovered_at)
        max_active = max(max_active, w_last_active)

        mints_by_w = sum(1 for l_rec in launches if l_rec.creator_wallet == w.address)
        parent_tx = repository.get_parent_transfer(w.address) if repository else None
        direct_funding = parent_tx.amount_sol if parent_tx else 0.0
        rel = (
            "FUNDED"
            if (w.parent_wallet == root_address or direct_funding > 0)
            else "SHARED"
        )

        w_roles = {WalletRole.SATELLITE}

        transfers_by_w = [
            t
            for t in (repository.get_transfers() if repository else ())
            if w.address in (t.from_wallet, t.to_wallet)
        ]
        distinct_creators_funded = len(
            {
                t.from_wallet
                for t in transfers_by_w
                if t.to_wallet == w.address and t.from_wallet != root_address
            }
        )
        is_repeat_bundler = (mints_by_w == 0 and distinct_creators_funded >= 2) or (
            mints_by_w == 0 and len(transfers_by_w) >= 3 and direct_funding < 5.0
        )

        is_enrolled = (
            (repository.get_funder(w.address) is not None) if repository else False
        )
        policy = (
            repository.get_target_execution_policy(w.address)
            if repository and is_enrolled
            else None
        )
        is_armed = (
            policy is not None
            and policy.monitoring_enabled
            and policy.execution_mode.value in ("live", "simulated", "paper")
        )

        if is_armed:
            stage = "ARMED"
            next_action = "ARM"
        elif is_enrolled:
            stage = "TRACKED"
            next_action = "BACKTEST"
        else:
            stage = "NEW"
            next_action = "TRACK"

        # Deterministic User-Oriented Role Classification
        if mints_by_w > 0:
            role = "Creator"
            w_roles.add(WalletRole.CREATOR)
        elif is_repeat_bundler:
            role = "Bundler"
            w_roles.add(WalletRole.BUYER)
        elif direct_funding > 5.0:
            role = "Treasury"
            w_roles.add(WalletRole.DUMPER)
        elif mints_by_w == 0 and 0.2 <= direct_funding <= 5.0:
            role = "Launch"
            w_roles.add(WalletRole.CREATOR)
        elif mints_by_w == 0 and 0.001 <= direct_funding < 0.2:
            role = "Buyer"
            w_roles.add(WalletRole.BUYER)
        elif direct_funding > 0 or rel == "FUNDED":
            role = "Buyer"
            w_roles.add(WalletRole.BUYER)
        else:
            role = "Satellite"

        # Derive funding path: check if funded via intermediate (treasury/satellite)
        if w.parent_wallet and w.parent_wallet != root_address:
            link_path = "Root → Treasury → Wallet"
        elif rel == "FUNDED" or direct_funding > 0:
            link_path = "Root → Wallet"
        else:
            link_path = "Root → … → Wallet"

        rows.append(
            WalletDiscoveredRow(
                address=w.address,
                label=f"Sat {w.address[:4]}..",
                relation=rel,
                last_active_epoch=w_last_active,
                mints_count=mints_by_w,
                roles=frozenset(w_roles),
                role=role,
                stage=stage,
                next_action=next_action,
                behavior_str=role,
                direct_funding_sol=direct_funding,
                depth=w.depth,
                deploy_probability_pct=95
                if role == "Launch"
                else (90 if role == "Bundler" else (75 if role == "Buyer" else 10)),
                stage_status=role,
                root_address=root_address,
                link_path=link_path,
            )
        )

    # Sort so Fresh Launch Wallets & Staged Wallets appear at the very top of the table
    def _sort_wallet_rank(row: WalletDiscoveredRow) -> tuple[int, int, float, int]:
        role_priority = {
            "Launch": 100,
            "Bundler": 90,
            "Buyer": 75,
            "Creator": 50,
            "Treasury": 30,
            "Satellite": 10,
            "Root": 5,
        }.get(row.role, 0)
        return (
            role_priority,
            row.last_active_epoch,
            row.direct_funding_sol,
            1 if row.relation == "ROOT" else 0,
        )

    sorted_rows = sorted(rows, key=_sort_wallet_rank, reverse=True)
    return sorted_rows, max_active


def _build_dossiers(
    root_address: str,
    discovered_wallets: list[WalletDiscoveredRow],
    launches: tuple[LaunchRecord, ...],
    transfers: tuple[TransferRecord, ...] | list[TransferRecord],
    top_candidate_address: str | None = None,
) -> dict[str, WalletDossier]:
    dossiers: dict[str, WalletDossier] = {}
    cluster_size = len(discovered_wallets)

    for w_row in discovered_wallets:
        addr = w_row.address
        is_root = addr == root_address
        is_top_cand = addr == top_candidate_address

        tokens_list = _build_wallet_tokens(addr, launches)
        funding_list = _build_wallet_funding(addr, root_address, is_root, transfers)
        coordination_list = _build_wallet_coordination(
            root_address, is_root, w_row.depth, launches
        )
        signals = _build_wallet_signals(is_root, w_row, launches, cluster_size)

        bought_count = sum(1 for t in tokens_list if t.role == "Bought")
        sold_count = sum(1 for t in tokens_list if t.wallet_exit_str != "—")

        relation_summary = WalletRelationSummary(
            direct_funding_sol=w_row.direct_funding_sol,
            shared_funding_source=w_row.relation in ("FUNDED", "SHARED"),
            shared_launches_count=len(tokens_list),
            same_slot_activity_count=1 if w_row.depth <= 1 and not is_root else 0,
        )

        behavior_summary = WalletBehaviorSummary(
            created_tokens_count=w_row.mints_count,
            bought_launches_count=bought_count,
            sold_launches_count=sold_count,
            last_active_epoch=w_row.last_active_epoch,
        )

        dossiers[addr] = WalletDossier(
            address=addr,
            label=w_row.label,
            role=w_row.role,
            stage=w_row.stage,
            next_action=w_row.next_action,
            roles_str=w_row.role,
            direct_funding_sol=w_row.direct_funding_sol,
            last_active_epoch=w_row.last_active_epoch,
            relation=relation_summary,
            behavior=behavior_summary,
            signals=signals,
            deploy_probability_pct=w_row.deploy_probability_pct,
            stage_status=w_row.role,
            is_next_deployer_candidate=is_top_cand,
            tokens=tokens_list,
            funding=funding_list,
            coordination=coordination_list,
            root_address=root_address,
            link_path=w_row.link_path,
        )

    return dossiers


def build_cluster_intelligence_model(
    repository: SQLiteTrackerRepository | None,
    root_address: str,
    root_label: str = "Root Dev",
) -> ClusterIntelligenceModel:
    """Build the unified intelligence database for a tracked developer cluster."""
    model = ClusterIntelligenceModel(root_address=root_address, root_label=root_label)
    if not root_address:
        return model

    funder = repository.get_funder(root_address) if repository else None
    canonical_root_label = (
        (funder.label if funder and funder.label else None) or root_label or "Root Dev"
    )
    model.root_label = canonical_root_label

    launches: tuple[LaunchRecord, ...] = ()
    wallets: tuple[WalletRecord, ...] = ()
    transfers = repository.get_transfers(limit=300) if repository else ()

    if repository is not None:
        launches = repository.get_launches_for_funder(root_address)
        wallets = tuple(
            w for w in repository.get_wallets() if w.root_funder == root_address
        )

    root_last_active = _parse_epoch(funder.last_seen_at) if funder else 0
    if not root_last_active and funder:
        root_last_active = _parse_epoch(funder.created_at)

    model.token_count = len(launches)
    creators_set: set[str] = {
        launch_rec.creator_wallet
        for launch_rec in launches
        if launch_rec.creator_wallet
    }
    if launches:
        creators_set.add(root_address)
    model.creator_count = len(creators_set)
    model.total_wallets = 1 + len([w for w in wallets if w.address != root_address])

    root_roles = {WalletRole.ROOT}
    if launches:
        root_roles.add(WalletRole.CREATOR)
        root_roles.add(WalletRole.FUNDER)
    root_row = WalletDiscoveredRow(
        address=root_address,
        label=canonical_root_label,
        relation="ROOT",
        last_active_epoch=root_last_active,
        mints_count=len(launches),
        roles=frozenset(root_roles),
        role="Root",
        stage="TRACKED",
        next_action="BACKTEST",
        behavior_str="Root",
        direct_funding_sol=0.0,
        depth=0,
        deploy_probability_pct=10,
        stage_status="Root",
        root_address=root_address,
        link_path="Root",
    )

    discovered, max_active = _build_discovered_wallets(
        repository,
        root_row,
        launches,
        wallets,
    )
    model.discovered_wallets = discovered
    model.last_active_epoch = max_active

    staged = [
        w
        for w in discovered
        if (w.role in ("Launch", "Buyer") and w.address != root_address)
    ]
    model.staged_wallets_count = len(staged)
    model.bundler_wallets_count = len([w for w in discovered if w.role == "Bundler"])
    top_cand = next(
        (w for w in discovered if w.role == "Launch"),
        None,
    )
    if top_cand:
        model.top_candidate_address = top_cand.address
        model.top_candidate_funding_sol = top_cand.direct_funding_sol
        model.top_candidate_funded_epoch = top_cand.last_active_epoch

    model.dossiers = _build_dossiers(
        root_address,
        discovered,
        launches,
        transfers,
        model.top_candidate_address,
    )
    model.raw_evidence = _build_raw_evidence_rows(root_address, transfers, launches)
    return model


def build_candidate_rows(
    repository: SQLiteTrackerRepository | None,
    root_address: str,
) -> list[WalletDiscoveredRow]:
    """Lightweight projection: candidate rows for a scope root without full dossiers.

    Used to populate the global candidate list in ALL scope without building
    expensive per-wallet token/funding/coordination histories.
    """
    if not root_address or repository is None:
        return []

    funder = repository.get_funder(root_address)
    root_label = (funder.label if funder and funder.label else None) or "Root Dev"

    launches = repository.get_launches_for_funder(root_address)
    wallets = tuple(
        w for w in repository.get_wallets() if w.root_funder == root_address
    )

    root_last_active = _parse_epoch(funder.last_seen_at) if funder else 0
    if not root_last_active and funder:
        root_last_active = _parse_epoch(funder.created_at)

    root_roles = {WalletRole.ROOT}
    if launches:
        root_roles.add(WalletRole.CREATOR)
        root_roles.add(WalletRole.FUNDER)

    root_row = WalletDiscoveredRow(
        address=root_address,
        label=root_label,
        relation="ROOT",
        last_active_epoch=root_last_active,
        mints_count=len(launches),
        roles=frozenset(root_roles),
        role="Root",
        stage="TRACKED",
        next_action="BACKTEST",
        behavior_str="Root",
        direct_funding_sol=0.0,
        depth=0,
        deploy_probability_pct=10,
        stage_status="Root",
        root_address=root_address,
        link_path="Root",
    )

    rows, _ = _build_discovered_wallets(repository, root_row, launches, wallets)
    return rows
