import sqlite3
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from config import DB_PATH


def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = get_connection()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS agents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            starting_capital REAL NOT NULL DEFAULT 100000,
            cash REAL NOT NULL DEFAULT 100000,
            risk_level INTEGER NOT NULL DEFAULT 5,
            max_position_pct REAL NOT NULL DEFAULT 20,
            max_options_pct REAL NOT NULL DEFAULT 10,
            min_cash_reserve_pct REAL NOT NULL DEFAULT 10,
            max_daily_loss_pct REAL NOT NULL DEFAULT 3,
            max_daily_investment_pct REAL NOT NULL DEFAULT 40,
            allowed_asset_types TEXT NOT NULL DEFAULT 'stock,etf,mutual_fund,commodity,option',
            model TEXT NOT NULL DEFAULT 'claude-sonnet-4-20250514',
            scout_model TEXT NOT NULL DEFAULT 'claude-haiku-4-20250414',
            check_interval_minutes INTEGER NOT NULL DEFAULT 15,
            strategy_interval_minutes INTEGER NOT NULL DEFAULT 60,
            current_strategy TEXT DEFAULT '',
            strategy_updated_at TEXT DEFAULT '',
            last_run_at TEXT DEFAULT '',
            test_group TEXT DEFAULT '',
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            asset_type TEXT NOT NULL,
            quantity REAL NOT NULL,
            avg_cost REAL NOT NULL,
            UNIQUE(agent_id, symbol, asset_type),
            FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id INTEGER NOT NULL,
            timestamp TEXT NOT NULL,
            symbol TEXT NOT NULL,
            asset_type TEXT NOT NULL,
            action TEXT NOT NULL,
            quantity REAL NOT NULL,
            price REAL NOT NULL,
            total_cost REAL NOT NULL,
            realized_pnl REAL DEFAULT 0,
            avg_cost_basis REAL DEFAULT 0,
            reasoning TEXT DEFAULT '',
            FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS daily_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            total_value REAL NOT NULL,
            cash REAL NOT NULL,
            invested REAL NOT NULL,
            daily_return_pct REAL DEFAULT 0,
            total_return_pct REAL DEFAULT 0,
            sp500_total_return_pct REAL DEFAULT 0,
            num_positions INTEGER DEFAULT 0,
            UNIQUE(agent_id, date),
            FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS intraday_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id INTEGER NOT NULL,
            timestamp TEXT NOT NULL,
            total_value REAL NOT NULL,
            cash REAL NOT NULL,
            invested REAL NOT NULL,
            num_positions INTEGER DEFAULT 0,
            session_phase TEXT DEFAULT '',
            FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS agent_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id INTEGER NOT NULL,
            timestamp TEXT NOT NULL,
            run_type TEXT DEFAULT 'scheduled',
            summary TEXT DEFAULT '',
            full_log TEXT DEFAULT '',
            trades_made INTEGER DEFAULT 0,
            FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS token_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            agent_id INTEGER,
            model TEXT NOT NULL DEFAULT 'claude-sonnet-4-20250514',
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            cost_usd REAL NOT NULL DEFAULT 0,
            FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE SET NULL
        );
    """)

    # Migration: add allowed_asset_types column if missing (for existing databases)
    cols = [row[1] for row in conn.execute("PRAGMA table_info(agents)").fetchall()]
    if "allowed_asset_types" not in cols:
        conn.execute(
            "ALTER TABLE agents ADD COLUMN allowed_asset_types TEXT NOT NULL DEFAULT 'stock,etf,mutual_fund,commodity,option'"
        )

    # Migration: add model column to agents
    if "model" not in cols:
        conn.execute(
            "ALTER TABLE agents ADD COLUMN model TEXT NOT NULL DEFAULT 'claude-sonnet-4-20250514'"
        )

    # Migration: two-model architecture columns
    if "scout_model" not in cols:
        conn.execute("ALTER TABLE agents ADD COLUMN scout_model TEXT NOT NULL DEFAULT 'claude-haiku-4-20250414'")
    if "check_interval_minutes" not in cols:
        conn.execute("ALTER TABLE agents ADD COLUMN check_interval_minutes INTEGER NOT NULL DEFAULT 15")
    if "strategy_interval_minutes" not in cols:
        conn.execute("ALTER TABLE agents ADD COLUMN strategy_interval_minutes INTEGER NOT NULL DEFAULT 60")
    if "current_strategy" not in cols:
        conn.execute("ALTER TABLE agents ADD COLUMN current_strategy TEXT DEFAULT ''")
    if "strategy_updated_at" not in cols:
        conn.execute("ALTER TABLE agents ADD COLUMN strategy_updated_at TEXT DEFAULT ''")
    if "last_run_at" not in cols:
        conn.execute("ALTER TABLE agents ADD COLUMN last_run_at TEXT DEFAULT ''")
    if "test_group" not in cols:
        conn.execute("ALTER TABLE agents ADD COLUMN test_group TEXT DEFAULT ''")

    # Migration: add realized_pnl and avg_cost_basis to trades
    trade_cols = [row[1] for row in conn.execute("PRAGMA table_info(trades)").fetchall()]
    if "realized_pnl" not in trade_cols:
        conn.execute("ALTER TABLE trades ADD COLUMN realized_pnl REAL DEFAULT 0")
    if "avg_cost_basis" not in trade_cols:
        conn.execute("ALTER TABLE trades ADD COLUMN avg_cost_basis REAL DEFAULT 0")

    # Migration: add model column to token_usage
    tu_cols = [row[1] for row in conn.execute("PRAGMA table_info(token_usage)").fetchall()]
    if "model" not in tu_cols:
        conn.execute(
            "ALTER TABLE token_usage ADD COLUMN model TEXT NOT NULL DEFAULT 'claude-sonnet-4-20250514'"
        )

    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Agent CRUD
# ---------------------------------------------------------------------------

RISK_PRESETS = {
    1:  {"max_position_pct": 8,  "max_options_pct": 0,  "min_cash_reserve_pct": 25, "max_daily_loss_pct": 1,   "max_daily_investment_pct": 15},
    2:  {"max_position_pct": 10, "max_options_pct": 0,  "min_cash_reserve_pct": 20, "max_daily_loss_pct": 1.5, "max_daily_investment_pct": 20},
    3:  {"max_position_pct": 12, "max_options_pct": 5,  "min_cash_reserve_pct": 15, "max_daily_loss_pct": 2,   "max_daily_investment_pct": 25},
    4:  {"max_position_pct": 15, "max_options_pct": 8,  "min_cash_reserve_pct": 12, "max_daily_loss_pct": 2.5, "max_daily_investment_pct": 30},
    5:  {"max_position_pct": 20, "max_options_pct": 10, "min_cash_reserve_pct": 10, "max_daily_loss_pct": 3,   "max_daily_investment_pct": 40},
    6:  {"max_position_pct": 22, "max_options_pct": 12, "min_cash_reserve_pct": 8,  "max_daily_loss_pct": 3.5, "max_daily_investment_pct": 45},
    7:  {"max_position_pct": 25, "max_options_pct": 15, "min_cash_reserve_pct": 7,  "max_daily_loss_pct": 4,   "max_daily_investment_pct": 55},
    8:  {"max_position_pct": 30, "max_options_pct": 20, "min_cash_reserve_pct": 5,  "max_daily_loss_pct": 5,   "max_daily_investment_pct": 65},
    9:  {"max_position_pct": 35, "max_options_pct": 25, "min_cash_reserve_pct": 3,  "max_daily_loss_pct": 7,   "max_daily_investment_pct": 80},
    10: {"max_position_pct": 40, "max_options_pct": 30, "min_cash_reserve_pct": 2,  "max_daily_loss_pct": 10,  "max_daily_investment_pct": 100},
}


ALL_ASSET_TYPES = ["stock", "etf", "mutual_fund", "commodity", "option"]


SUPPORTED_MODELS = [
    # Anthropic
    "claude-sonnet-4-20250514",
    "claude-haiku-4-20250414",
    "claude-opus-4-20250514",
    # OpenAI
    "gpt-4o",
    "gpt-4o-mini",
    "o3-mini",
    # Google
    "gemini-2.5-pro",
    "gemini-2.5-flash",
]

DEFAULT_MODEL = "claude-sonnet-4-20250514"
DEFAULT_SCOUT_MODEL = "claude-haiku-4-20250414"

CHEAP_MODELS = {"claude-haiku-4-20250414", "gpt-4o-mini", "o3-mini", "gemini-2.5-flash"}
EXPENSIVE_MODELS = {"claude-sonnet-4-20250514", "claude-opus-4-20250514", "gpt-4o", "gemini-2.5-pro"}

MODEL_SHORT_NAMES = {
    "claude-haiku-4-20250414": "Haiku",
    "claude-sonnet-4-20250514": "Sonnet",
    "claude-opus-4-20250514": "Opus",
    "gpt-4o": "GPT4o",
    "gpt-4o-mini": "Mini",
    "o3-mini": "o3mini",
    "gemini-2.5-pro": "GemPro",
    "gemini-2.5-flash": "Flash",
}


def get_model_section(scout_model: str, decision_model: str) -> str:
    scout_cheap = scout_model in CHEAP_MODELS
    decision_cheap = decision_model in CHEAP_MODELS
    if scout_cheap and not decision_cheap:
        return "Cheap Scout + Expensive Decision"
    if not scout_cheap and not decision_cheap:
        return "Expensive Scout + Expensive Decision"
    if scout_cheap and decision_cheap:
        return "Cheap Scout + Cheap Decision"
    return "Expensive Scout + Cheap Decision"


def create_agent(
    name: str,
    starting_capital: float = 100000,
    risk_level: int = 5,
    max_position_pct: float = None,
    max_options_pct: float = None,
    min_cash_reserve_pct: float = None,
    max_daily_loss_pct: float = None,
    max_daily_investment_pct: float = None,
    allowed_asset_types: list = None,
    model: str = None,
    scout_model: str = None,
    check_interval_minutes: int = 15,
    strategy_interval_minutes: int = 60,
    test_group: str = "",
) -> int:
    risk_level = max(1, min(10, risk_level))
    preset = RISK_PRESETS[risk_level]

    if allowed_asset_types is None:
        asset_types_str = ",".join(ALL_ASSET_TYPES)
    else:
        asset_types_str = ",".join(t for t in allowed_asset_types if t in ALL_ASSET_TYPES)
        if not asset_types_str:
            asset_types_str = ",".join(ALL_ASSET_TYPES)

    agent_model = model if model in SUPPORTED_MODELS else DEFAULT_MODEL
    agent_scout_model = scout_model if scout_model in SUPPORTED_MODELS else DEFAULT_SCOUT_MODEL
    check_interval_minutes = max(2, min(60, check_interval_minutes))
    strategy_interval_minutes = max(15, min(240, strategy_interval_minutes))

    conn = get_connection()
    cur = conn.execute(
        """INSERT INTO agents (name, starting_capital, cash, risk_level,
           max_position_pct, max_options_pct, min_cash_reserve_pct,
           max_daily_loss_pct, max_daily_investment_pct, allowed_asset_types,
           model, scout_model, check_interval_minutes, strategy_interval_minutes,
           test_group, is_active, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
        (
            name,
            starting_capital,
            starting_capital,
            risk_level,
            max_position_pct if max_position_pct is not None else preset["max_position_pct"],
            max_options_pct if max_options_pct is not None else preset["max_options_pct"],
            min_cash_reserve_pct if min_cash_reserve_pct is not None else preset["min_cash_reserve_pct"],
            max_daily_loss_pct if max_daily_loss_pct is not None else preset["max_daily_loss_pct"],
            max_daily_investment_pct if max_daily_investment_pct is not None else preset["max_daily_investment_pct"],
            asset_types_str,
            agent_model,
            agent_scout_model,
            check_interval_minutes,
            strategy_interval_minutes,
            test_group,
            datetime.now().isoformat(),
        ),
    )
    agent_id = cur.lastrowid
    conn.commit()
    conn.close()
    return agent_id


def get_agent(agent_id: int) -> Optional[dict]:
    conn = get_connection()
    row = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_all_agents() -> list[dict]:
    conn = get_connection()
    rows = conn.execute("SELECT * FROM agents ORDER BY created_at ASC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_active_agents() -> list[dict]:
    conn = get_connection()
    rows = conn.execute("SELECT * FROM agents WHERE is_active = 1 ORDER BY created_at ASC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_agents_by_test_group(test_group: str) -> list[dict]:
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM agents WHERE test_group = ? ORDER BY id ASC", (test_group,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_test_groups() -> list[str]:
    """Return all distinct non-empty test group names."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT DISTINCT test_group FROM agents WHERE test_group != '' ORDER BY test_group DESC"
    ).fetchall()
    conn.close()
    return [r["test_group"] for r in rows]


def get_agent_trade_stats(agent_id: int) -> dict:
    conn = get_connection()
    total = conn.execute(
        "SELECT COUNT(*) as cnt FROM trades WHERE agent_id = ?", (agent_id,)
    ).fetchone()["cnt"]
    sells = conn.execute(
        "SELECT COUNT(*) as cnt, "
        "SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins, "
        "SUM(CASE WHEN realized_pnl < 0 THEN 1 ELSE 0 END) as losses "
        "FROM trades WHERE agent_id = ? AND action = 'sell'",
        (agent_id,),
    ).fetchone()
    wins = sells["wins"] or 0
    losses = sells["losses"] or 0
    win_rate = (wins / (wins + losses) * 100) if (wins + losses) > 0 else 0
    conn.close()
    return {"total_trades": total, "wins": wins, "losses": losses, "win_rate": round(win_rate, 1)}


def get_agent_api_cost(agent_id: int) -> float:
    conn = get_connection()
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) as cost FROM token_usage WHERE agent_id = ?",
        (agent_id,),
    ).fetchone()
    conn.close()
    return round(row["cost"], 4)


def get_provider_costs(agent_ids: list[int]) -> dict:
    """Get API costs grouped by provider (claude, openai, gemini) for a set of agents."""
    if not agent_ids:
        return {"claude": 0, "openai": 0, "gemini": 0}
    conn = get_connection()
    placeholders = ",".join("?" * len(agent_ids))
    rows = conn.execute(
        f"SELECT model, COALESCE(SUM(cost_usd), 0) as cost "
        f"FROM token_usage WHERE agent_id IN ({placeholders}) GROUP BY model",
        agent_ids,
    ).fetchall()
    conn.close()
    totals = {"claude": 0.0, "openai": 0.0, "gemini": 0.0}
    for r in rows:
        model = r["model"] or ""
        if model.startswith("claude-"):
            totals["claude"] += r["cost"]
        elif model.startswith("gpt-") or model.startswith("o3"):
            totals["openai"] += r["cost"]
        elif model.startswith("gemini-"):
            totals["gemini"] += r["cost"]
    return {k: round(v, 4) for k, v in totals.items()}


def update_agent(agent_id: int, updates: dict):
    allowed = {
        "name", "risk_level", "max_position_pct", "max_options_pct",
        "min_cash_reserve_pct", "max_daily_loss_pct", "max_daily_investment_pct",
        "allowed_asset_types", "model", "scout_model", "check_interval_minutes",
        "strategy_interval_minutes", "test_group", "is_active",
    }
    filtered = {}
    for k, v in updates.items():
        if k not in allowed:
            continue
        if k == "allowed_asset_types" and isinstance(v, list):
            v = ",".join(t for t in v if t in ALL_ASSET_TYPES)
        if k == "check_interval_minutes":
            v = max(2, min(60, int(v)))
        if k == "strategy_interval_minutes":
            v = max(15, min(240, int(v)))
        filtered[k] = v

    if not filtered:
        return

    set_clause = ", ".join(f"{k} = ?" for k in filtered)
    values = list(filtered.values()) + [agent_id]

    conn = get_connection()
    conn.execute(f"UPDATE agents SET {set_clause} WHERE id = ?", values)
    conn.commit()
    conn.close()


def delete_agent(agent_id: int):
    conn = get_connection()
    conn.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
    conn.commit()
    conn.close()


def reset_agent(agent_id: int):
    """Wipe an agent's portfolio data but keep its settings."""
    agent = get_agent(agent_id)
    if not agent:
        return

    conn = get_connection()
    conn.execute("DELETE FROM positions WHERE agent_id = ?", (agent_id,))
    conn.execute("DELETE FROM trades WHERE agent_id = ?", (agent_id,))
    conn.execute("DELETE FROM daily_snapshots WHERE agent_id = ?", (agent_id,))
    conn.execute("DELETE FROM agent_logs WHERE agent_id = ?", (agent_id,))
    conn.execute(
        "UPDATE agents SET cash = ?, current_strategy = '', strategy_updated_at = '', last_run_at = '', created_at = ? WHERE id = ?",
        (agent["starting_capital"], datetime.now().isoformat(), agent_id),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Agent cash helpers
# ---------------------------------------------------------------------------

def get_cash(agent_id: int) -> float:
    conn = get_connection()
    row = conn.execute("SELECT cash FROM agents WHERE id = ?", (agent_id,)).fetchone()
    conn.close()
    return row["cash"] if row else 0.0


def update_cash(agent_id: int, new_cash: float):
    conn = get_connection()
    conn.execute("UPDATE agents SET cash = ? WHERE id = ?", (new_cash, agent_id))
    conn.commit()
    conn.close()


def get_starting_capital(agent_id: int) -> float:
    conn = get_connection()
    row = conn.execute("SELECT starting_capital FROM agents WHERE id = ?", (agent_id,)).fetchone()
    conn.close()
    return row["starting_capital"] if row else 100000


def update_agent_strategy(agent_id: int, strategy: str):
    """Store the latest strategy directive for an agent."""
    conn = get_connection()
    conn.execute(
        "UPDATE agents SET current_strategy = ?, strategy_updated_at = ? WHERE id = ?",
        (strategy, datetime.now().isoformat(), agent_id),
    )
    conn.commit()
    conn.close()


def update_last_run_at(agent_id: int):
    """Record that an agent just ran."""
    conn = get_connection()
    conn.execute(
        "UPDATE agents SET last_run_at = ? WHERE id = ?",
        (datetime.now().isoformat(), agent_id),
    )
    conn.commit()
    conn.close()


def get_allowed_asset_types(agent_id: int) -> list[str]:
    agent = get_agent(agent_id)
    if not agent:
        return ALL_ASSET_TYPES
    raw = agent.get("allowed_asset_types", "")
    if not raw:
        return ALL_ASSET_TYPES
    return [t.strip() for t in raw.split(",") if t.strip()]


def get_risk_params(agent_id: int) -> dict:
    agent = get_agent(agent_id)
    if not agent:
        return {**RISK_PRESETS[5], "allowed_asset_types": ALL_ASSET_TYPES}
    return {
        "risk_level": agent["risk_level"],
        "max_position_pct": agent["max_position_pct"] / 100,
        "max_options_pct": agent["max_options_pct"] / 100,
        "min_cash_reserve_pct": agent["min_cash_reserve_pct"] / 100,
        "max_daily_loss_pct": agent["max_daily_loss_pct"] / 100,
        "max_daily_investment_pct": agent["max_daily_investment_pct"] / 100,
        "allowed_asset_types": get_allowed_asset_types(agent_id),
    }


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------

def get_positions(agent_id: int) -> list[dict]:
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM positions WHERE agent_id = ? AND quantity > 0", (agent_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def upsert_position(agent_id: int, symbol: str, asset_type: str, quantity: float, avg_cost: float):
    conn = get_connection()
    if quantity <= 0:
        conn.execute(
            "DELETE FROM positions WHERE agent_id = ? AND symbol = ? AND asset_type = ?",
            (agent_id, symbol, asset_type),
        )
    else:
        conn.execute(
            """INSERT INTO positions (agent_id, symbol, asset_type, quantity, avg_cost)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(agent_id, symbol, asset_type)
               DO UPDATE SET quantity = ?, avg_cost = ?""",
            (agent_id, symbol, asset_type, quantity, avg_cost, quantity, avg_cost),
        )
    conn.commit()
    conn.close()


def get_position(agent_id: int, symbol: str, asset_type: str) -> Optional[dict]:
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM positions WHERE agent_id = ? AND symbol = ? AND asset_type = ?",
        (agent_id, symbol, asset_type),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_realized_pnl_today(agent_id: int) -> Optional[float]:
    """Sum of realized P&L from sell trades today."""
    today = datetime.now().strftime("%Y-%m-%d")
    conn = get_connection()
    row = conn.execute(
        "SELECT COALESCE(SUM(realized_pnl), 0) as total FROM trades "
        "WHERE agent_id = ? AND action = 'sell' AND timestamp >= ?",
        (agent_id, today),
    ).fetchone()
    conn.close()
    return float(row["total"]) if row else None


def get_last_trade_price(agent_id: int, symbol: str) -> Optional[float]:
    """Get the most recent trade price for a symbol from trade history."""
    conn = get_connection()
    row = conn.execute(
        "SELECT price FROM trades WHERE agent_id = ? AND symbol = ? ORDER BY timestamp DESC LIMIT 1",
        (agent_id, symbol),
    ).fetchone()
    conn.close()
    return float(row["price"]) if row else None


# ---------------------------------------------------------------------------
# Trades
# ---------------------------------------------------------------------------

def insert_trade(
    agent_id: int,
    symbol: str,
    asset_type: str,
    action: str,
    quantity: float,
    price: float,
    total_cost: float,
    reasoning: str = "",
    realized_pnl: float = 0,
    avg_cost_basis: float = 0,
):
    conn = get_connection()
    conn.execute(
        """INSERT INTO trades (agent_id, timestamp, symbol, asset_type, action, quantity, price, total_cost, realized_pnl, avg_cost_basis, reasoning)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (agent_id, datetime.now().isoformat(), symbol, asset_type, action, quantity, price, total_cost, realized_pnl, avg_cost_basis, reasoning),
    )
    conn.commit()
    conn.close()


def get_trades(
    agent_id: int,
    limit: int = 50,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> list[dict]:
    query = "SELECT * FROM trades WHERE agent_id = ?"
    params: list = [agent_id]
    if start_date:
        query += " AND timestamp >= ?"
        params.append(start_date)
    if end_date:
        query += " AND timestamp < ?"
        params.append(end_date + "T23:59:59" if "T" not in end_date else end_date)
    query += " ORDER BY timestamp DESC LIMIT ?"
    params.append(limit)
    conn = get_connection()
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------

def insert_daily_snapshot(
    agent_id: int,
    date: str,
    total_value: float,
    cash: float,
    invested: float,
    daily_return_pct: float,
    total_return_pct: float,
    sp500_total_return_pct: float,
    num_positions: int,
):
    conn = get_connection()
    conn.execute(
        """INSERT INTO daily_snapshots (agent_id, date, total_value, cash, invested,
           daily_return_pct, total_return_pct, sp500_total_return_pct, num_positions)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(agent_id, date) DO UPDATE SET
               total_value = ?, cash = ?, invested = ?,
               daily_return_pct = ?, total_return_pct = ?,
               sp500_total_return_pct = ?, num_positions = ?""",
        (
            agent_id, date, total_value, cash, invested,
            daily_return_pct, total_return_pct, sp500_total_return_pct, num_positions,
            total_value, cash, invested,
            daily_return_pct, total_return_pct, sp500_total_return_pct, num_positions,
        ),
    )
    conn.commit()
    conn.close()


def get_daily_snapshots(
    agent_id: int,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> list[dict]:
    query = "SELECT * FROM daily_snapshots WHERE agent_id = ?"
    params: list = [agent_id]
    if start_date:
        query += " AND date >= ?"
        params.append(start_date)
    if end_date:
        query += " AND date <= ?"
        params.append(end_date)
    query += " ORDER BY date ASC"
    conn = get_connection()
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_all_agents_snapshots(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> list[dict]:
    query = "SELECT * FROM daily_snapshots WHERE 1=1"
    params: list = []
    if start_date:
        query += " AND date >= ?"
        params.append(start_date)
    if end_date:
        query += " AND date <= ?"
        params.append(end_date)
    query += " ORDER BY date ASC, agent_id ASC"
    conn = get_connection()
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_latest_snapshot(agent_id: int) -> Optional[dict]:
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM daily_snapshots WHERE agent_id = ? ORDER BY date DESC LIMIT 1",
        (agent_id,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_previous_day_snapshot(agent_id: int) -> Optional[dict]:
    """Get yesterday's closing snapshot (not today's)."""
    from datetime import date
    today = date.today().isoformat()
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM daily_snapshots WHERE agent_id = ? AND date < ? ORDER BY date DESC LIMIT 1",
        (agent_id, today),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Intraday snapshots
# ---------------------------------------------------------------------------

def insert_intraday_snapshot(
    agent_id: int,
    total_value: float,
    cash: float,
    invested: float,
    num_positions: int,
    session_phase: str = "",
):
    conn = get_connection()
    conn.execute(
        """INSERT INTO intraday_snapshots (agent_id, timestamp, total_value, cash, invested, num_positions, session_phase)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (agent_id, datetime.now().isoformat(), total_value, cash, invested, num_positions, session_phase),
    )
    conn.commit()
    conn.close()


def get_intraday_snapshots(agent_id: int, date: Optional[str] = None) -> list[dict]:
    """Get intraday snapshots for an agent, optionally filtered to a specific date."""
    query = "SELECT * FROM intraday_snapshots WHERE agent_id = ?"
    params: list = [agent_id]
    if date:
        query += " AND timestamp LIKE ?"
        params.append(f"{date}%")
    query += " ORDER BY timestamp ASC"
    conn = get_connection()
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Agent logs
# ---------------------------------------------------------------------------

def insert_agent_log(
    agent_id: int, run_type: str, summary: str, full_log: str, trades_made: int
):
    conn = get_connection()
    conn.execute(
        """INSERT INTO agent_logs (agent_id, timestamp, run_type, summary, full_log, trades_made)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (agent_id, datetime.now().isoformat(), run_type, summary, full_log, trades_made),
    )
    conn.commit()
    conn.close()


def get_agent_logs(agent_id: int, limit: int = 20) -> list[dict]:
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM agent_logs WHERE agent_id = ? ORDER BY timestamp DESC LIMIT ?",
        (agent_id, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Token usage tracking
# ---------------------------------------------------------------------------

# Per-model pricing: (input_cost_per_million_tokens, output_cost_per_million_tokens)
MODEL_PRICING = {
    # Anthropic
    "claude-sonnet-4-20250514":  (3.00,  15.00),
    "claude-haiku-4-20250414":   (0.80,   4.00),
    "claude-opus-4-20250514":    (15.00,  75.00),
    # OpenAI (for future use)
    "gpt-4o":                    (2.50,  10.00),
    "gpt-4o-mini":               (0.15,   0.60),
    "o3-mini":                   (1.10,   4.40),
    # Google (for future use)
    "gemini-2.5-pro":            (1.25,  10.00),
    "gemini-2.5-flash":          (0.15,   0.60),
}

FALLBACK_PRICING = (3.00, 15.00)


def _calc_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    input_rate, output_rate = MODEL_PRICING.get(model, FALLBACK_PRICING)
    return (input_tokens / 1_000_000 * input_rate +
            output_tokens / 1_000_000 * output_rate)


def insert_token_usage(agent_id: int, input_tokens: int, output_tokens: int,
                        model: str = "claude-sonnet-4-20250514"):
    cost = _calc_cost(model, input_tokens, output_tokens)
    conn = get_connection()
    conn.execute(
        """INSERT INTO token_usage (timestamp, agent_id, model, input_tokens, output_tokens, cost_usd)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (datetime.now().isoformat(), agent_id, model, input_tokens, output_tokens, round(cost, 6)),
    )
    conn.commit()
    conn.close()


def get_token_cost_summary() -> dict:
    conn = get_connection()
    today = datetime.now().strftime("%Y-%m-%d")

    from datetime import timedelta
    now = datetime.now()
    week_start = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
    month_start = now.strftime("%Y-%m-01")

    def _sum(where: str = "", params: tuple = ()) -> dict:
        row = conn.execute(
            f"SELECT COALESCE(SUM(input_tokens),0) as inp, "
            f"COALESCE(SUM(output_tokens),0) as outp, "
            f"COALESCE(SUM(cost_usd),0) as cost "
            f"FROM token_usage {where}", params
        ).fetchone()
        return {"input_tokens": row["inp"], "output_tokens": row["outp"], "cost": round(row["cost"], 4)}

    def _by_model(where: str = "", params: tuple = ()) -> list:
        time_filter = f"AND {where.replace('WHERE ', '')}" if where else ""
        rows = conn.execute(
            f"SELECT model, "
            f"COALESCE(SUM(input_tokens),0) as inp, "
            f"COALESCE(SUM(output_tokens),0) as outp, "
            f"COALESCE(SUM(cost_usd),0) as cost, "
            f"COUNT(*) as calls "
            f"FROM token_usage WHERE 1=1 {time_filter} "
            f"GROUP BY model ORDER BY cost DESC", params
        ).fetchall()
        return [
            {"model": r["model"], "input_tokens": r["inp"], "output_tokens": r["outp"],
             "cost": round(r["cost"], 4), "calls": r["calls"]}
            for r in rows
        ]

    result = {
        "lifetime": _sum(),
        "month": _sum("WHERE timestamp >= ?", (month_start,)),
        "week": _sum("WHERE timestamp >= ?", (week_start,)),
        "today": _sum("WHERE timestamp >= ?", (today,)),
        "by_model": _by_model(),
        "by_model_today": _by_model("WHERE timestamp >= ?", (today,)),
    }
    conn.close()
    return result
