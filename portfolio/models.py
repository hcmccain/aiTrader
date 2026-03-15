from pydantic import BaseModel
from datetime import datetime
from enum import Enum
from typing import Optional


class AssetType(str, Enum):
    STOCK = "stock"
    ETF = "etf"
    OPTION = "option"
    CRYPTO = "crypto"
    BOND = "bond"


class TradeAction(str, Enum):
    BUY = "buy"
    SELL = "sell"


class Position(BaseModel):
    id: Optional[int] = None
    symbol: str
    asset_type: AssetType
    quantity: float
    avg_cost: float
    current_price: float = 0.0
    market_value: float = 0.0
    unrealized_pnl: float = 0.0
    unrealized_pnl_pct: float = 0.0


class Trade(BaseModel):
    id: Optional[int] = None
    timestamp: datetime
    symbol: str
    asset_type: AssetType
    action: TradeAction
    quantity: float
    price: float
    total_cost: float
    reasoning: str = ""


class DailySnapshot(BaseModel):
    id: Optional[int] = None
    date: str
    total_value: float
    cash: float
    invested: float
    daily_return_pct: float = 0.0
    total_return_pct: float = 0.0
    sp500_total_return_pct: float = 0.0
    num_positions: int = 0


class PortfolioSummary(BaseModel):
    total_value: float
    cash: float
    invested_value: float
    total_return: float
    total_return_pct: float
    daily_return: float
    daily_return_pct: float
    num_positions: int
    positions: list[Position]


class AgentLog(BaseModel):
    id: Optional[int] = None
    timestamp: datetime
    run_type: str = "scheduled"
    summary: str = ""
    full_log: str = ""
    trades_made: int = 0
