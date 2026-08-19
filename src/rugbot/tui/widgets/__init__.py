"""TUI reusable widgets package."""

from rugbot.tui.widgets.activity import LiveActivityView
from rugbot.tui.widgets.header import CompactHeader
from rugbot.tui.widgets.inspector import EventInspector
from rugbot.tui.widgets.modal import DetailInspectModal
from rugbot.tui.widgets.quick_buy_modal import QuickBuyModal, QuickBuyOrder
from rugbot.tui.widgets.sell_modal import FastSellModal, FastSellOrder
from rugbot.tui.widgets.watching import FunderCardInfo, WatchingView

__all__ = [
    "CompactHeader",
    "DetailInspectModal",
    "EventInspector",
    "FastSellModal",
    "FastSellOrder",
    "FunderCardInfo",
    "LiveActivityView",
    "QuickBuyModal",
    "QuickBuyOrder",
    "WatchingView",
]
