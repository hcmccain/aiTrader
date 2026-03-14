import logging
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from config import SCHEDULE_HOUR, SCHEDULE_MINUTE, INTRADAY_INTERVAL_MINUTES

logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler()


def _stop_loss_monitor():
    """Lightweight price check every 30 seconds — auto-sells positions down more than 3%."""
    from data.market import is_market_open, get_current_price
    from portfolio.database import get_active_agents, get_connection
    from portfolio.manager import get_portfolio_summary, execute_trade
    from portfolio.models import AssetType, TradeAction

    if not is_market_open():
        return

    agents = get_active_agents()
    for agent in agents:
        try:
            summary = get_portfolio_summary(agent["id"])
            for p in summary.positions:
                if p.quantity <= 0:
                    continue
                mult = 100 if p.asset_type == AssetType.OPTION else 1
                cost = p.avg_cost * p.quantity * mult
                if cost <= 0:
                    continue
                price = get_current_price(p.symbol)
                if price is None:
                    continue
                current_value = price * p.quantity * mult
                pnl_pct = (current_value - cost) / cost
                if pnl_pct < -0.03:
                    ok, msg = execute_trade(
                        agent["id"], p.symbol, p.asset_type, TradeAction.SELL,
                        p.quantity, price,
                        f"Auto stop-loss (monitor): position down {pnl_pct*100:.1f}%"
                    )
                    if ok:
                        logger.info(
                            f"[STOP-LOSS] {agent['name']}: Sold {p.symbol} "
                            f"(down {pnl_pct*100:.1f}%, loss ${(current_value - cost):,.2f})"
                        )
        except Exception as e:
            logger.error(f"Stop-loss monitor error for '{agent['name']}': {e}")


def _run_active_agents_job():
    """Run all active agents."""
    from agent.trader import run_trading_session
    from portfolio.database import get_active_agents
    from data.market import get_market_session_phase

    phase = get_market_session_phase()
    if phase == "closed":
        return

    agents = get_active_agents()
    if not agents:
        logger.info("No active agents to run")
        return

    logger.info(f"[{phase}] Running session for {len(agents)} active agent(s)")
    for agent in agents:
        try:
            result = run_trading_session(agent["id"], run_type="scheduled", session_phase=phase)
            logger.info(f"Agent '{agent['name']}': {result.get('trades_made', 0)} trades ({phase})")
        except Exception as e:
            logger.error(f"Agent '{agent['name']}' failed: {e}", exc_info=True)


def _eod_snapshot_job():
    from portfolio.manager import take_daily_snapshot
    from portfolio.database import get_active_agents

    agents = get_active_agents()
    for agent in agents:
        try:
            take_daily_snapshot(agent["id"])
            logger.info(f"EOD snapshot for '{agent['name']}'")
        except Exception as e:
            logger.error(f"EOD snapshot failed for '{agent['name']}': {e}")


def _build_cron_minutes(interval: int) -> str:
    """Build a cron minute spec aligned to market open (minute 0 and 30 for 30-min, etc.)."""
    minutes = list(range(0, 60, interval))
    if 30 not in minutes:
        minutes.append(30)
        minutes.sort()
    return ",".join(str(m) for m in minutes)


def start_scheduler():
    if scheduler.running:
        logger.info("Scheduler already running, skipping start")
        return

    trading_minutes = _build_cron_minutes(5)

    # Active agents: every 5 min
    scheduler.add_job(
        _run_active_agents_job,
        CronTrigger(
            day_of_week="mon-fri",
            hour="9-15",
            minute=trading_minutes,
            timezone="US/Eastern",
        ),
        id="intraday_trading",
        name="Active Agents (every 5 min)",
        replace_existing=True,
    )

    # Stop-loss monitor: every 30 seconds during market hours
    scheduler.add_job(
        _stop_loss_monitor,
        IntervalTrigger(seconds=30),
        id="stop_loss_monitor",
        name="Stop-Loss Monitor (every 30s)",
        replace_existing=True,
    )

    # End-of-day snapshot at 4:05 PM ET
    scheduler.add_job(
        _eod_snapshot_job,
        CronTrigger(day_of_week="mon-fri", hour=16, minute=5, timezone="US/Eastern"),
        id="eod_snapshot",
        name="End-of-Day Snapshot",
        replace_existing=True,
    )

    scheduler.start()
    logger.info(
        f"Scheduler started — active agents every 5 min, stop-loss monitor every 30s, EOD snapshot at 4:05 PM ET"
    )


def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown()
        logger.info("Scheduler stopped")
