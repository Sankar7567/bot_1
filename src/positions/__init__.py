"""Position Management package for Binance USD(S)-M Futures."""

from src.positions.exceptions import (
    PositionError,
    PositionLockError,
    PositionNotFoundError,
)
from src.positions.manager import PositionManager
from src.positions.models import (
    EmergencyExitResult,
    ExitReason,
    ExitTriggerResult,
    Position,
    PositionState,
    ProtectiveOrderPair,
)
from src.positions.protective import (
    ProtectiveOrderManager,
    round_price_directional,
)

__all__ = [
    "EmergencyExitResult",
    "ExitReason",
    "ExitTriggerResult",
    "Position",
    "PositionError",
    "PositionLockError",
    "PositionManager",
    "PositionNotFoundError",
    "PositionState",
    "ProtectiveOrderPair",
    "ProtectiveOrderManager",
    "round_price_directional",
]
