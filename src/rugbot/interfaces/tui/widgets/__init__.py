"""Terminal user interface widgets, cockpit panels, and interactive modals."""

from __future__ import annotations

from rugbot.interfaces.tui.widgets.modals.graph_modal import ClusterGraphModal
from rugbot.interfaces.tui.widgets.modals.help_modal import HelpCheatsheetScreen
from rugbot.interfaces.tui.widgets.modals.modal import DetailInspectModal
from rugbot.interfaces.tui.widgets.modals.quick_buy_modal import (
    QuickBuyModal,
    QuickBuyOrder,
)
from rugbot.interfaces.tui.widgets.modals.sell_modal import FastSellModal, FastSellOrder
from rugbot.interfaces.tui.widgets.panels.activity import (
    ActivityItem,
    EmptyStateView,
    LiveActivityView,
)
from rugbot.interfaces.tui.widgets.panels.backtest_matrix_view import (
    BacktestMatrixWidget,
)
from rugbot.interfaces.tui.widgets.panels.cluster_graph_view import ClusterGraphWidget
from rugbot.interfaces.tui.widgets.panels.execution_rail import ExecutionCard
from rugbot.interfaces.tui.widgets.panels.header import CompactHeader
from rugbot.interfaces.tui.widgets.panels.inspector import (
    DevHistoryCard,
    EventInspector,
    EventLogTicker,
    OperatorStage,
    RiskBar,
    TargetProfileCard,
    TokenDetailCard,
)
from rugbot.interfaces.tui.widgets.panels.pnl import WalletPnlHistory, WalletPnlPanel
from rugbot.interfaces.tui.widgets.panels.position_panel import PositionExecutionPanel
from rugbot.interfaces.tui.widgets.panels.targets_table import TargetsTable
from rugbot.interfaces.tui.widgets.panels.wallet_risk import WalletRiskPanel
from rugbot.interfaces.tui.widgets.panels.watching import FunderCardInfo, WatchingView

__all__ = [
    "ActivityItem",
    "BacktestMatrixWidget",
    "ClusterGraphModal",
    "ClusterGraphWidget",
    "CompactHeader",
    "DetailInspectModal",
    "DevHistoryCard",
    "EmptyStateView",
    "EventInspector",
    "EventLogTicker",
    "ExecutionCard",
    "FastSellModal",
    "FastSellOrder",
    "FunderCardInfo",
    "HelpCheatsheetScreen",
    "LiveActivityView",
    "OperatorStage",
    "PositionExecutionPanel",
    "QuickBuyModal",
    "QuickBuyOrder",
    "RiskBar",
    "TargetProfileCard",
    "TargetsTable",
    "TokenDetailCard",
    "WalletPnlHistory",
    "WalletPnlPanel",
    "WalletRiskPanel",
    "WatchingView",
]
