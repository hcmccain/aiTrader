import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse

from config import ANTHROPIC_API_KEY, WEB_HOST, WEB_PORT
from portfolio.manager import initialize, get_portfolio_summary, take_daily_snapshot
from typing import Optional

from portfolio.database import (
    get_all_agents,
    get_active_agents,
    get_agent,
    create_agent,
    update_agent,
    delete_agent,
    reset_agent,
    get_daily_snapshots,
    get_all_agents_snapshots,
    get_intraday_snapshots,
    get_trades,
    get_agent_logs,
    get_token_cost_summary,
    get_agents_by_test_group,
    get_test_groups,
    get_agent_trade_stats,
    get_agent_api_cost,
    RISK_PRESETS,
    ALL_ASSET_TYPES,
    SUPPORTED_MODELS,
    MODEL_SHORT_NAMES,
    get_model_section,
)
from scheduler.jobs import start_scheduler, stop_scheduler
from web.events import subscribe

logger = logging.getLogger(__name__)


def _serialize_agent(agent: dict) -> dict:
    """Convert stored agent dict so allowed_asset_types is a list for the API."""
    a = dict(agent)
    raw = a.get("allowed_asset_types", "")
    if isinstance(raw, str):
        a["allowed_asset_types"] = [t.strip() for t in raw.split(",") if t.strip()]
    return a


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Initializing database...")
    initialize()

    if not ANTHROPIC_API_KEY:
        logger.warning(
            "ANTHROPIC_API_KEY not set — agents won't run. "
            "Add it to your .env file and restart."
        )
    else:
        logger.info("Starting scheduler...")
        start_scheduler()

    logger.info(f"Dashboard available at http://{WEB_HOST}:{WEB_PORT}")
    yield
    stop_scheduler()
    logger.info("Shutting down.")


app = FastAPI(title="AI Portfolio Trader", lifespan=lifespan)

templates_dir = Path(__file__).parent / "templates"
static_dir = templates_dir / "static"
templates = Jinja2Templates(directory=str(templates_dir))
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request})


# ---------------------------------------------------------------------------
# Agent CRUD API
# ---------------------------------------------------------------------------

@app.get("/api/agents")
async def api_list_agents(exclude_test: bool = True):
    try:
        agents = get_all_agents()
        if exclude_test:
            agents = [a for a in agents if not a.get("test_group")]
        result = []
        for agent in agents:
            try:
                summary = get_portfolio_summary(agent["id"])
                agent["total_value"] = summary.total_value
                agent["total_return"] = summary.total_return
                agent["total_return_pct"] = summary.total_return_pct
                agent["daily_return"] = summary.daily_return
                agent["daily_return_pct"] = summary.daily_return_pct
                agent["invested_value"] = summary.invested_value
                agent["num_positions"] = summary.num_positions
            except Exception:
                agent["total_value"] = agent["cash"]
                agent["total_return"] = 0
                agent["total_return_pct"] = 0
                agent["daily_return"] = 0
                agent["daily_return_pct"] = 0
                agent["invested_value"] = 0
                agent["num_positions"] = 0
            result.append(_serialize_agent(agent))
        return result
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/agents")
async def api_create_agent(request: Request):
    try:
        body = await request.json()
        name = body.get("name", "").strip()
        if not name:
            return JSONResponse({"error": "Agent name is required"}, status_code=400)

        capital = float(body.get("starting_capital", 100000))
        if capital < 1000:
            return JSONResponse({"error": "Starting capital must be at least $1,000"}, status_code=400)

        risk_level = int(body.get("risk_level", 5))

        agent_id = create_agent(
            name=name,
            starting_capital=capital,
            risk_level=risk_level,
            max_position_pct=body.get("max_position_pct"),
            max_options_pct=body.get("max_options_pct"),
            min_cash_reserve_pct=body.get("min_cash_reserve_pct"),
            max_daily_loss_pct=body.get("max_daily_loss_pct"),
            max_daily_investment_pct=body.get("max_daily_investment_pct"),
            allowed_asset_types=body.get("allowed_asset_types"),
            model=body.get("model"),
            scout_model=body.get("scout_model"),
            check_interval_minutes=int(body.get("check_interval_minutes", 15)),
            strategy_interval_minutes=int(body.get("strategy_interval_minutes", 60)),
        )

        return {"status": "ok", "agent_id": agent_id, "agent": _serialize_agent(get_agent(agent_id))}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.put("/api/agents/{agent_id}")
async def api_update_agent(agent_id: int, request: Request):
    try:
        agent = get_agent(agent_id)
        if not agent:
            return JSONResponse({"error": "Agent not found"}, status_code=404)

        body = await request.json()
        update_agent(agent_id, body)
        return {"status": "ok", "agent": _serialize_agent(get_agent(agent_id))}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.delete("/api/agents/{agent_id}")
async def api_delete_agent(agent_id: int):
    try:
        agent = get_agent(agent_id)
        if not agent:
            return JSONResponse({"error": "Agent not found"}, status_code=404)

        delete_agent(agent_id)
        return {"status": "ok"}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/agents/{agent_id}/reset")
async def api_reset_agent(agent_id: int):
    try:
        agent = get_agent(agent_id)
        if not agent:
            return JSONResponse({"error": "Agent not found"}, status_code=404)

        reset_agent(agent_id)
        return {"status": "ok", "message": f"Agent '{agent['name']}' reset"}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/agents/{agent_id}/risk-presets")
async def api_risk_presets():
    return RISK_PRESETS


# ---------------------------------------------------------------------------
# Per-agent data API
# ---------------------------------------------------------------------------

@app.get("/api/agents/{agent_id}/summary")
async def api_agent_summary(agent_id: int):
    try:
        agent = get_agent(agent_id)
        if not agent:
            return JSONResponse({"error": "Agent not found"}, status_code=404)

        summary = get_portfolio_summary(agent_id)
        return {
            "agent": _serialize_agent(agent),
            "total_value": summary.total_value,
            "cash": summary.cash,
            "invested_value": summary.invested_value,
            "total_return": summary.total_return,
            "total_return_pct": summary.total_return_pct,
            "daily_return": summary.daily_return,
            "daily_return_pct": summary.daily_return_pct,
            "num_positions": summary.num_positions,
            "starting_capital": agent["starting_capital"],
            "positions": [p.model_dump() for p in summary.positions],
        }
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/agents/{agent_id}/snapshots")
async def api_agent_snapshots(
    agent_id: int,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
):
    try:
        return get_daily_snapshots(agent_id, start_date=start_date, end_date=end_date)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/snapshots/all")
async def api_all_snapshots(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
):
    try:
        return get_all_agents_snapshots(start_date=start_date, end_date=end_date)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/agents/{agent_id}/trades")
async def api_agent_trades(
    agent_id: int,
    limit: int = 200,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
):
    try:
        return get_trades(agent_id, limit=limit, start_date=start_date, end_date=end_date)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/agents/{agent_id}/logs")
async def api_agent_logs(agent_id: int, limit: int = 20):
    try:
        return get_agent_logs(agent_id, limit=limit)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/agents/{agent_id}/run")
async def api_run_agent(agent_id: int):
    from agent.trader import run_trading_session
    from data.market import get_market_session_phase
    try:
        agent = get_agent(agent_id)
        if not agent:
            return JSONResponse({"error": "Agent not found"}, status_code=404)

        phase = get_market_session_phase()
        if phase == "closed":
            phase = "morning"
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, lambda: run_trading_session(agent_id, run_type="manual", session_phase=phase)
        )
        return result
    except Exception as e:
        logger.error(f"Manual run failed: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/run-all")
async def api_run_all_agents():
    from agent.trader import run_trading_session
    from data.market import get_market_session_phase
    try:
        phase = get_market_session_phase()
        if phase == "closed":
            phase = "morning"
        agents = get_active_agents()
        loop = asyncio.get_event_loop()

        results = []
        for agent in agents:
            try:
                result = await loop.run_in_executor(
                    None, lambda a=agent: run_trading_session(a["id"], run_type="manual", session_phase=phase)
                )
                results.append(result)
            except Exception as e:
                results.append({"agent_id": agent["id"], "agent_name": agent["name"], "error": str(e)})
        return {"results": results}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# Server-Sent Events for real-time dashboard updates
# ---------------------------------------------------------------------------

@app.get("/api/events")
async def api_sse_events(request: Request):
    async def event_generator():
        async for data in subscribe():
            if await request.is_disconnected():
                break
            yield {"data": data}

    return EventSourceResponse(event_generator())


@app.get("/api/agents/{agent_id}/intraday-snapshots")
async def api_intraday_snapshots(agent_id: int, date: Optional[str] = None):
    try:
        return get_intraday_snapshots(agent_id, date=date)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/config/intraday")
async def api_intraday_config():
    from data.market import is_market_open, get_market_session_phase, get_sessions_remaining_today
    return {
        "interval_minutes": 15,
        "market_open": is_market_open(),
        "session_phase": get_market_session_phase(),
        "sessions_remaining": get_sessions_remaining_today(),
    }


@app.get("/api/token-costs")
async def api_token_costs():
    try:
        return get_token_cost_summary()
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


_company_name_cache: dict[str, str] = {}

@app.get("/api/company/{ticker}")
async def api_company_name(ticker: str):
    ticker = ticker.upper()
    if ticker in _company_name_cache:
        return {"ticker": ticker, "name": _company_name_cache[ticker]}
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        name = getattr(t, "info", {}).get("longName") or getattr(t, "info", {}).get("shortName") or ticker
        _company_name_cache[ticker] = name
        return {"ticker": ticker, "name": name}
    except Exception:
        return {"ticker": ticker, "name": ticker}


# ---------------------------------------------------------------------------
# Model Testing Matrix API
# ---------------------------------------------------------------------------

@app.post("/api/agents/generate-test-matrix")
async def api_generate_test_matrix(request: Request):
    from datetime import date as dt_date
    try:
        body = await request.json()
        capital = float(body.get("starting_capital", 10000))
        risk_level = int(body.get("risk_level", 5))
        check_interval = int(body.get("check_interval_minutes", 5))
        strategy_interval = int(body.get("strategy_interval_minutes", 60))
        test_group = body.get("test_group", f"matrix-{dt_date.today().isoformat()}")

        scout_models = body.get("scout_models", SUPPORTED_MODELS)
        decision_models = body.get("decision_models", SUPPORTED_MODELS)
        scout_models = [m for m in scout_models if m in SUPPORTED_MODELS]
        decision_models = [m for m in decision_models if m in SUPPORTED_MODELS]
        if not scout_models or not decision_models:
            return JSONResponse({"error": "Select at least one scout and one decision model"}, status_code=400)

        existing = get_agents_by_test_group(test_group)
        existing_names = {a["name"] for a in existing}

        created = []
        skipped = 0
        for scout in scout_models:
            for decision in decision_models:
                scout_short = MODEL_SHORT_NAMES.get(scout, scout)
                decision_short = MODEL_SHORT_NAMES.get(decision, decision)
                name = f"{scout_short}\u2192{decision_short}"

                if name in existing_names:
                    skipped += 1
                    continue

                agent_id = create_agent(
                    name=name,
                    starting_capital=capital,
                    risk_level=risk_level,
                    model=decision,
                    scout_model=scout,
                    check_interval_minutes=check_interval,
                    strategy_interval_minutes=strategy_interval,
                    test_group=test_group,
                )
                created.append({"id": agent_id, "name": name, "scout": scout, "decision": decision})

        return {
            "status": "ok",
            "test_group": test_group,
            "created": len(created),
            "skipped": skipped,
            "agents": created,
        }
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/test-matrix")
async def api_test_matrix(test_group: Optional[str] = None):
    try:
        if not test_group:
            groups = get_test_groups()
            if not groups:
                return {"agents": [], "test_group": None, "test_groups": []}
            test_group = groups[0]

        agents = get_agents_by_test_group(test_group)
        result = []
        for agent in agents:
            scout = agent.get("scout_model", "")
            decision = agent.get("model", "")
            section = get_model_section(scout, decision)

            try:
                summary = get_portfolio_summary(agent["id"])
                total_return_pct = summary.total_return_pct
                daily_return_pct = summary.daily_return_pct
                total_value = summary.total_value
            except Exception:
                total_return_pct = 0
                daily_return_pct = 0
                total_value = agent["cash"]

            stats = get_agent_trade_stats(agent["id"])
            api_cost = get_agent_api_cost(agent["id"])
            total_return_dollar = total_value - agent["starting_capital"]
            roi_net = total_return_dollar - api_cost

            result.append({
                "id": agent["id"],
                "name": agent["name"],
                "scout_model": scout,
                "decision_model": decision,
                "scout_short": MODEL_SHORT_NAMES.get(scout, scout),
                "decision_short": MODEL_SHORT_NAMES.get(decision, decision),
                "section": section,
                "is_active": agent["is_active"],
                "starting_capital": agent["starting_capital"],
                "total_value": total_value,
                "total_return_pct": total_return_pct,
                "daily_return_pct": daily_return_pct,
                "total_trades": stats["total_trades"],
                "wins": stats["wins"],
                "losses": stats["losses"],
                "win_rate": stats["win_rate"],
                "api_cost": api_cost,
                "roi_net": round(roi_net, 2),
                "check_interval_minutes": agent.get("check_interval_minutes", 15),
            })

        result.sort(key=lambda x: x["total_return_pct"], reverse=True)

        return {
            "agents": result,
            "test_group": test_group,
            "test_groups": get_test_groups(),
        }
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/test-matrix/snapshots")
async def api_test_matrix_snapshots(
    test_group: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
):
    try:
        if not test_group:
            groups = get_test_groups()
            if not groups:
                return {"snapshots": {}, "test_group": None}
            test_group = groups[0]

        agents = get_agents_by_test_group(test_group)
        snapshots = {}
        for agent in agents:
            snaps = get_daily_snapshots(agent["id"], start_date=start_date, end_date=end_date)
            if snaps:
                snapshots[agent["id"]] = {
                    "name": agent["name"],
                    "scout_short": MODEL_SHORT_NAMES.get(agent.get("scout_model", ""), ""),
                    "decision_short": MODEL_SHORT_NAMES.get(agent.get("model", ""), ""),
                    "section": get_model_section(
                        agent.get("scout_model", ""), agent.get("model", "")
                    ),
                    "data": snaps,
                }

        return {"snapshots": snapshots, "test_group": test_group}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.delete("/api/test-matrix/{test_group}")
async def api_delete_test_matrix(test_group: str):
    try:
        agents = get_agents_by_test_group(test_group)
        for agent in agents:
            delete_agent(agent["id"])
        return {"status": "ok", "deleted": len(agents)}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# Trade Exports (CSV + PDF)
# ---------------------------------------------------------------------------

def _get_all_trades(agent_id: int) -> list[dict]:
    from portfolio.database import get_connection
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM trades WHERE agent_id = ? ORDER BY timestamp ASC", (agent_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/agents/{agent_id}/export/csv")
async def api_export_csv(agent_id: int):
    import csv
    import io

    agent = get_agent(agent_id)
    if not agent:
        return JSONResponse({"error": "Agent not found"}, status_code=404)

    trades = _get_all_trades(agent_id)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "Date/Time", "Symbol", "Asset Type", "Action", "Quantity",
        "Price", "Total Cost", "Avg Cost Basis", "Realized P&L", "Reasoning",
    ])
    for t in trades:
        writer.writerow([
            t["timestamp"], t["symbol"], t["asset_type"], t["action"].upper(),
            t["quantity"], f'{t["price"]:.4f}', f'{t["total_cost"]:.2f}',
            f'{t.get("avg_cost_basis", 0):.4f}',
            f'{t.get("realized_pnl", 0):.2f}',
            t.get("reasoning", ""),
        ])

    buf.seek(0)
    safe_name = agent["name"].replace(" ", "_").lower()
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}_trades.csv"'},
    )


@app.get("/api/agents/{agent_id}/export/pdf")
async def api_export_pdf(agent_id: int):
    import io
    from fpdf import FPDF

    agent = get_agent(agent_id)
    if not agent:
        return JSONResponse({"error": "Agent not found"}, status_code=404)

    trades = _get_all_trades(agent_id)
    summary = get_portfolio_summary(agent_id)

    pdf = FPDF(orientation="L", format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    # Title
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, f"Trade Report: {agent['name']}", new_x="LMARGIN", new_y="NEXT")

    # Summary section
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 6,
        f"Starting Capital: ${agent['starting_capital']:,.2f}   |   "
        f"Current Value: ${summary.total_value:,.2f}   |   "
        f"Total Return: ${summary.total_return:+,.2f} ({summary.total_return_pct:+.2f}%)   |   "
        f"Risk Level: {agent['risk_level']}/10",
        new_x="LMARGIN", new_y="NEXT",
    )
    pdf.cell(0, 6,
        f"Total Trades: {len(trades)}   |   "
        f"Cash: ${summary.cash:,.2f}   |   "
        f"Invested: ${summary.invested_value:,.2f}   |   "
        f"Positions: {summary.num_positions}",
        new_x="LMARGIN", new_y="NEXT",
    )
    pdf.ln(4)

    # Table header
    col_widths = [36, 52, 22, 14, 16, 22, 28, 24, 60]
    headers = ["Date/Time", "Symbol", "Type", "Side", "Qty", "Price", "Total", "P&L", "Reasoning"]

    pdf.set_font("Helvetica", "B", 8)
    pdf.set_fill_color(40, 50, 70)
    pdf.set_text_color(220, 220, 220)
    for i, h in enumerate(headers):
        pdf.cell(col_widths[i], 7, h, border=1, fill=True, align="C")
    pdf.ln()

    # Table rows
    pdf.set_font("Helvetica", "", 7)
    pdf.set_text_color(30, 30, 30)

    total_realized = 0
    wins = 0
    losses = 0

    for idx, t in enumerate(trades):
        rpnl = t.get("realized_pnl", 0) or 0
        if t["action"] == "sell" and rpnl != 0:
            total_realized += rpnl
            if rpnl > 0:
                wins += 1
            else:
                losses += 1

        bg = (245, 245, 245) if idx % 2 == 0 else (255, 255, 255)
        pdf.set_fill_color(*bg)

        ts = t["timestamp"][:16].replace("T", " ") if t.get("timestamp") else ""
        reason = (t.get("reasoning") or "")[:80]

        row = [
            ts,
            t["symbol"],
            t["asset_type"],
            t["action"].upper(),
            str(int(t["quantity"])),
            f'${t["price"]:.2f}',
            f'${t["total_cost"]:,.2f}',
            f'${rpnl:+,.2f}' if t["action"] == "sell" and rpnl else "",
            reason,
        ]

        for i, val in enumerate(row):
            align = "R" if i in (4, 5, 6, 7) else "L"
            pdf.cell(col_widths[i], 5.5, val, border=1, fill=True, align=align)
        pdf.ln()

    # Summary footer
    pdf.ln(6)
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_text_color(30, 30, 30)
    pdf.cell(0, 7,
        f"Total Realized P&L: ${total_realized:+,.2f}   |   "
        f"Wins: {wins}   |   Losses: {losses}   |   "
        f"Win Rate: {(wins/(wins+losses)*100) if (wins+losses) > 0 else 0:.1f}%",
        new_x="LMARGIN", new_y="NEXT",
    )

    buf = io.BytesIO()
    pdf.output(buf)
    buf.seek(0)

    safe_name = agent["name"].replace(" ", "_").lower()
    return StreamingResponse(
        buf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}_trade_report.pdf"'},
    )
