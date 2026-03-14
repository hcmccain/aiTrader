import json
import logging
from datetime import datetime

import anthropic

from config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL
from agent.prompts import build_system_prompt
from agent.tools import TOOL_DEFINITIONS, handle_tool_call
from portfolio.manager import take_intraday_snapshot
from portfolio.database import insert_agent_log, get_agent, insert_token_usage
from data.market import get_position_changes, get_sessions_remaining_today

logger = logging.getLogger(__name__)


def _build_user_message(agent_name: str, session_phase: str, position_changes: list[dict],
                        cash: float = 0, num_positions: int = 0, is_options_only: bool = False) -> str:
    """Build the user message with time-of-day context and position changes."""
    now_str = datetime.now().strftime("%A, %B %d, %Y at %I:%M %p")
    sessions_left = get_sessions_remaining_today()

    msg = (
        f"It is {now_str}. "
        f"Session phase: {session_phase.upper().replace('_', ' ')}. "
        f"Sessions remaining today: {sessions_left}. "
        f"You are managing the '{agent_name}' portfolio.\n\n"
    )

    if is_options_only and cash > 200:
        cash_pct = cash / (cash + sum(1 for _ in [])) * 100 if num_positions == 0 else 0
        msg += (
            f"**You have ${cash:,.0f} in cash and {num_positions} open position(s).** "
        )
        if cash > 1000 and num_positions < 3:
            msg += (
                f"You should be deploying more — aim for 4-6 positions. "
                f"Size each trade $200-$1,500 based on how strong the mover is. "
                f"Quick example: a $1.00 option x 8 contracts = $800 position.\n\n"
            )
        else:
            msg += "Check your positions — take profits on winners, cut losers, rotate into movers.\n\n"

    if position_changes:
        msg += "**Current positions since last check:**\n"
        for pc in position_changes:
            direction = "+" if pc["pnl"] >= 0 else ""
            msg += (
                f"- {pc['symbol']} ({pc['asset_type']}): {pc['qty']} units @ ${pc['avg_cost']} → "
                f"${pc['current']} ({direction}{pc['pnl_pct']:.1f}%, {direction}${pc['pnl']:.2f})\n"
            )
        msg += "\n"

    if session_phase == "pre_market":
        msg += "The market opens in minutes. Plan your opening trades. Start by reviewing your portfolio."
    elif session_phase == "morning":
        msg += "The market is open. Execute your best trades. Start by reviewing your portfolio."
    elif session_phase == "midday":
        msg += "Review your morning trades. Take profits where appropriate and look for new opportunities."
    elif session_phase == "afternoon":
        msg += "Afternoon session. Evaluate your positions and decide what to hold or sell before the close."
    elif session_phase == "closing":
        msg += "The market closes soon. Make final decisions — close day trades or hold overnight. Be decisive."
    else:
        msg += "Please analyze the current portfolio and market conditions, then make any trades you think are appropriate."

    return msg


def run_trading_session(agent_id: int, run_type: str = "scheduled", session_phase: str = "morning") -> dict:
    """Run a trading session for a specific agent. Shorter iterations for intraday check-ins."""
    agent = get_agent(agent_id)
    if not agent:
        return {"error": f"Agent {agent_id} not found"}

    agent_name = agent["name"]
    logger.info(f"Starting {run_type}/{session_phase} session for agent '{agent_name}' (id={agent_id})")

    if not ANTHROPIC_API_KEY:
        logger.error("ANTHROPIC_API_KEY not set")
        return {"error": "ANTHROPIC_API_KEY not configured"}

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    system_prompt = build_system_prompt(agent_id, session_phase=session_phase)

    max_iterations = 15

    conversation_log = []
    trades_made = 0

    position_changes = get_position_changes(agent_id)

    from portfolio.manager import get_portfolio_summary, execute_trade
    from portfolio.models import AssetType, TradeAction
    from portfolio.database import get_risk_params
    from data.market import get_current_price

    summary = get_portfolio_summary(agent_id)
    risk = get_risk_params(agent_id)
    allowed = risk.get("allowed_asset_types", [])
    is_options_only = allowed == ["option"]

    # Auto-cut losers before AI even runs
    for p in summary.positions:
        if p.quantity <= 0:
            continue
        cost = p.avg_cost * p.quantity * (100 if p.asset_type == AssetType.OPTION else 1)
        if cost > 0 and p.unrealized_pnl / cost < -0.03:
            price = get_current_price(p.symbol) or p.current_price
            ok, msg = execute_trade(
                agent_id, p.symbol, p.asset_type, TradeAction.SELL,
                p.quantity, price, "Auto stop-loss: position down more than 3%"
            )
            if ok:
                logger.info(f"[{agent_name}] AUTO STOP-LOSS: Sold {p.symbol} (down {p.unrealized_pnl/cost*100:.1f}%)")
                trades_made += 1

    summary = get_portfolio_summary(agent_id)

    user_message = _build_user_message(
        agent_name, session_phase, position_changes,
        cash=summary.cash, num_positions=summary.num_positions,
        is_options_only=is_options_only,
    )

    messages = [{"role": "user", "content": user_message}]
    conversation_log.append({"role": "user", "content": user_message})

    for iteration in range(max_iterations):
        logger.info(f"[{agent_name}] Iteration {iteration + 1}/{max_iterations} ({session_phase})")

        try:
            response = client.messages.create(
                model=ANTHROPIC_MODEL,
                max_tokens=4096,
                system=system_prompt,
                tools=TOOL_DEFINITIONS,
                messages=messages,
            )
        except Exception as e:
            logger.error(f"Anthropic API error: {e}")
            conversation_log.append({"role": "error", "content": str(e)})
            break

        try:
            usage = response.usage
            insert_token_usage(agent_id, usage.input_tokens, usage.output_tokens)
        except Exception:
            pass

        assistant_content = response.content
        messages.append({"role": "assistant", "content": assistant_content})

        tool_results = []
        text_parts = []

        for block in assistant_content:
            if block.type == "text":
                text_parts.append(block.text)
                conversation_log.append({"role": "assistant", "content": block.text})

            elif block.type == "tool_use":
                tool_name = block.name
                tool_input = block.input
                logger.info(f"[{agent_name}] Tool: {tool_name}({json.dumps(tool_input)[:200]})")

                conversation_log.append({
                    "role": "tool_call",
                    "tool": tool_name,
                    "input": tool_input,
                })

                result = handle_tool_call(tool_name, tool_input, agent_id)

                if tool_name == "place_trade":
                    result_data = json.loads(result)
                    if result_data.get("success"):
                        trades_made += 1
                        _fire_trade_event(agent_id, agent_name, result_data)

                conversation_log.append({
                    "role": "tool_result",
                    "tool": tool_name,
                    "result": result[:2000],
                })

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

        if response.stop_reason == "end_turn":
            logger.info(f"[{agent_name}] Finished (end_turn)")
            break

        if tool_results:
            messages.append({"role": "user", "content": tool_results})
        else:
            break

    summary_parts = [p for p in conversation_log if p.get("role") == "assistant"]
    summary = summary_parts[-1]["content"] if summary_parts else "No summary available"

    take_intraday_snapshot(agent_id)
    _fire_snapshot_event(agent_id, agent_name)

    insert_agent_log(
        agent_id=agent_id,
        run_type=f"{run_type}/{session_phase}",
        summary=summary[:1000],
        full_log=json.dumps(conversation_log, default=str)[:50000],
        trades_made=trades_made,
    )

    logger.info(f"[{agent_name}] Session complete ({session_phase}). Trades: {trades_made}")

    return {
        "agent_id": agent_id,
        "agent_name": agent_name,
        "trades_made": trades_made,
        "summary": summary,
        "iterations": min(iteration + 1, max_iterations),
        "session_phase": session_phase,
    }


# Keep backward compat for manual runs and existing API endpoints
def run_daily_trading_session(agent_id: int, run_type: str = "scheduled") -> dict:
    return run_trading_session(agent_id, run_type=run_type, session_phase="morning")


def _fire_trade_event(agent_id: int, agent_name: str, trade_data: dict):
    """Push a trade event to the SSE event bus."""
    try:
        from web.events import publish_event
        publish_event({
            "type": "trade_executed",
            "agent_id": agent_id,
            "agent_name": agent_name,
            "trade": trade_data,
        })
    except Exception:
        pass


def _fire_snapshot_event(agent_id: int, agent_name: str):
    """Push a snapshot event to the SSE event bus."""
    try:
        from web.events import publish_event
        publish_event({
            "type": "snapshot_updated",
            "agent_id": agent_id,
            "agent_name": agent_name,
        })
    except Exception:
        pass
