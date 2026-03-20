"""
Polymarket Copy-Trading Bot (Paper Mode)
=========================================
Reads signals from a Telegram channel (e.g. @PolyBeatsEN style),
resolves the Polymarket prediction market, and executes virtual paper trades.
Reports trade events and PnL summaries to a Telegram chat.

Signal format handled (PolyBeats channel style):
    New Address Wagers $26k That U.S. Troops Will Enter Iran This Year

    On the prediction market Polymarket, a new address has invested $26k in
    the "Yes" outcome for "Will U.S. troops enter Iran this year?"
    The current probability for this event is 71%.
    ...
    Total Investment: $26k
"""

import os
import re
import json
import asyncio
import logging
import aiohttp
import asyncpg
from datetime import datetime, timezone
from typing import Optional
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.tl.types import PeerChannel, Channel

# ─── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)-8s │ %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ─── Config ─────────────────────────────────────────────────────────────────
load_dotenv()

TELEGRAM_API_ID   = int(os.getenv("TELEGRAM_API_ID") or "0")
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID", "")
SIGNAL_CHANNEL    = os.getenv("SIGNAL_CHANNEL", "")      # e.g. @PolyBeatsEN or https://t.me/...
VIRTUAL_BALANCE   = float(os.getenv("VIRTUAL_BALANCE", "10000"))
DATABASE_URL      = os.getenv("DATABASE_URL")            # PostgreSQL connection string

# Polymarket public API endpoints (no auth required)
GAMMA_API   = "https://gamma-api.polymarket.com"
CLOB_API    = "https://clob.polymarket.com"

# PnL report interval (seconds)
PNL_REPORT_INTERVAL = 3600  # 1 hour

# ─── In-memory paper portfolio ───────────────────────────────────────────────
portfolio = {
    "balance_usd": VIRTUAL_BALANCE,
    "positions": {},   # { position_id: {...} }
    "closed_trades": [],
    "total_pnl": 0.0,
}
_position_counter = 0

# ─── Database ────────────────────────────────────────────────────────────────
db_pool = None

async def init_db():
    """Connect to PostgreSQL and ensure tables exist."""
    global db_pool, _position_counter
    if not DATABASE_URL:
        logger.warning("DATABASE_URL not set — bot will run in memory-only mode (data lost on restart).")
        return

    try:
        db_pool = await asyncpg.create_pool(DATABASE_URL)
        async with db_pool.acquire() as conn:
            # Create trades table
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS signals_trades (
                    id SERIAL PRIMARY KEY,
                    pos_id INT UNIQUE,
                    market_slug VARCHAR(255),
                    market_title TEXT,
                    side VARCHAR(10),
                    entry_price FLOAT,
                    amount_usd FLOAT,
                    shares FLOAT,
                    token_id VARCHAR(255),
                    status VARCHAR(20) DEFAULT 'OPEN',
                    unrealized_pnl FLOAT DEFAULT 0.0,
                    whale_wallet VARCHAR(255),
                    opened_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    closed_at TIMESTAMP WITH TIME ZONE
                )
            ''')
            # Create state table for balance
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS bot_state (
                    key VARCHAR(50) PRIMARY KEY,
                    value TEXT
                )
            ''')
            # Initialize balance in DB if not present
            val = await conn.fetchval("SELECT value FROM bot_state WHERE key = 'balance_usd'")
            if val is None:
                await conn.execute("INSERT INTO bot_state (key, value) VALUES ('balance_usd', $1)", str(VIRTUAL_BALANCE))
            else:
                portfolio["balance_usd"] = float(val)

            # Load open positions
            rows = await conn.fetch("SELECT * FROM signals_trades WHERE status = 'OPEN'")
            for row in rows:
                pos_id = row['pos_id']
                portfolio["positions"][pos_id] = {
                    "id": pos_id,
                    "market_slug": row['market_slug'],
                    "market_title": row['market_title'],
                    "side": row['side'],
                    "entry_price": row['entry_price'],
                    "entry_amount_usd": row['amount_usd'],
                    "shares": row['shares'],
                    "token_id": row['token_id'],
                    "opened_at": row['opened_at'].isoformat(),
                    "current_price": row['entry_price'],
                    "unrealized_pnl": row['unrealized_pnl'],
                    "whale_wallet": row['whale_wallet'],
                }
                _position_counter = max(_position_counter, pos_id)

            # Load recent closed trades
            closed_rows = await conn.fetch("SELECT * FROM signals_trades WHERE status = 'CLOSED' ORDER BY closed_at DESC LIMIT 20")
            for row in closed_rows:
                portfolio["closed_trades"].append({
                    "market_title": row['market_title'],
                    "side": row['side'],
                    "realized_pnl": row['unrealized_pnl'], # at close time
                })

        logger.info(f"✅ Database initialized. Loaded {len(portfolio['positions'])} open positions.")
    except Exception as e:
        logger.error(f"❌ Database connection error: {e}")
        db_pool = None

async def save_trade_to_db(pos: dict):
    if not db_pool: return
    try:
        async with db_pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO signals_trades 
                (pos_id, market_slug, market_title, side, entry_price, amount_usd, shares, token_id, whale_wallet, opened_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            ''', pos['id'], pos['market_slug'], pos['market_title'], pos['side'], 
                 pos['entry_price'], pos['entry_amount_usd'], pos['shares'], 
                 pos['token_id'], pos['whale_wallet'], datetime.fromisoformat(pos['opened_at']))
            
            # Update balance
            await conn.execute("UPDATE bot_state SET value = $1 WHERE key = 'balance_usd'", str(portfolio['balance_usd']))
    except Exception as e:
        logger.error(f"Error saving trade to DB: {e}")

async def update_db_pnl():
    if not db_pool: return
    try:
        async with db_pool.acquire() as conn:
            for pos in portfolio["positions"].values():
                await conn.execute('''
                    UPDATE signals_trades SET unrealized_pnl = $1 WHERE pos_id = $2
                ''', pos['unrealized_pnl'], pos['id'])
    except Exception as e:
        logger.error(f"Error updating PnL in DB: {e}")

# ─── Telegram sender (via Bot API) ──────────────────────────────────────────
async def send_telegram(session: aiohttp.ClientSession, text: str):
    """Send a message to TELEGRAM_CHAT_ID via Bot API."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram bot token / chat id not set — skipping notification.")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200:
                logger.warning(f"Telegram sendMessage returned {r.status}")
    except Exception as e:
        logger.error(f"Telegram send error: {e}")

# ─── Signal Parser ───────────────────────────────────────────────────────────
def parse_signal(text: str) -> Optional[dict]:
    """
    Parse a PolyBeats-style signal message.

    Returns dict with keys:
        market_query  – market title to search on Polymarket
        side          – "YES" or "NO"
        amount_usd    – dollar amount invested (float)
        probability   – current probability (float 0-1) or None
        wallet        – whale wallet (str) or None

    Returns None if the message doesn't look like a valid signal.
    """
    # Must mention Polymarket to be considered a signal
    if "polymarket" not in text.lower():
        return None

    # ── side: look for 'Yes' or 'No' outcome ─────────────────────────────────
    side_match = re.search(
        r'invested.*?in\s+the\s+"(Yes|No)"\s+outcome',
        text, re.IGNORECASE
    )
    if not side_match:
        # Fallback: first occurrence of bare "Yes"/"No" near "outcome"
        side_match = re.search(r'"(Yes|No)"\s+outcome', text, re.IGNORECASE)
    if not side_match:
        logger.info("Signal skipped — could not determine side (Yes/No).")
        return None
    side = side_match.group(1).upper()

    # ── market title: text inside quotes after "for" ─────────────────────────
    title_match = re.search(
        r'(?:outcome\s+for\s+|for the market\s+|market[:\s]+)"([^"]+)"',
        text, re.IGNORECASE
    )
    if not title_match:
        # Try: quotes anywhere after "outcome"
        title_match = re.search(r'outcome.*?"([^"]{10,})"', text, re.IGNORECASE | re.DOTALL)
    if not title_match:
        logger.info("Signal skipped — could not extract market title.")
        return None
    market_query = title_match.group(1).strip()

    # ── amount ────────────────────────────────────────────────────────────────
    # "Total Investment: $26k" or "invested $26k" or "$1.5M"
    amount_match = re.search(
        r'(?:Total Investment|invested|Wagers?)\s*:?\s*\$([0-9,.]+)\s*([kKmMbB]?)',
        text, re.IGNORECASE
    )
    if not amount_match:
        amount_match = re.search(r'\$([0-9,.]+)\s*([kKmMbB]?)', text)
    amount_usd = 0.0
    if amount_match:
        raw = float(amount_match.group(1).replace(",", ""))
        suffix = amount_match.group(2).upper()
        multipliers = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}
        amount_usd = raw * multipliers.get(suffix, 1)

    # ── probability ───────────────────────────────────────────────────────────
    prob_match = re.search(
        r'(?:current\s+probability|probability).*?(?:is|:)\s*([0-9]+(?:\.[0-9]+)?)\s*%',
        text, re.IGNORECASE
    )
    probability = float(prob_match.group(1)) / 100.0 if prob_match else None

    # ── wallet ────────────────────────────────────────────────────────────────
    wallet_match = re.search(r'0x[a-fA-F0-9]{40}', text)
    wallet = wallet_match.group(0) if wallet_match else None

    return {
        "market_query": market_query,
        "side": side,
        "amount_usd": amount_usd,
        "probability": probability,
        "wallet": wallet,
        "raw_text": text[:300],
    }

# ─── Polymarket Market Resolver ──────────────────────────────────────────────
async def resolve_market(session: aiohttp.ClientSession, query: str) -> Optional[dict]:
    """
    Search Polymarket Gamma API for a market matching `query`.
    Returns market dict with token_ids, slug, etc. or None.
    """
    try:
        params = {"q": query, "limit": 5, "active": "true", "closed": "false"}
        async with session.get(
            f"{GAMMA_API}/markets",
            params=params,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as r:
            r.raise_for_status()
            data = await r.json()
            markets = data if isinstance(data, list) else data.get("markets", [])
            if not markets:
                logger.warning(f"No markets found for query: '{query}'")
                return None
            # Pick the first active market
            return markets[0]
    except Exception as e:
        logger.error(f"Gamma API error: {e}")
        return None

async def get_market_price(session: aiohttp.ClientSession, token_id: str, side: str = "YES") -> Optional[float]:
    """
    Fetch midpoint price for a Polymarket token from CLOB API.
    side: 'YES' or 'NO'
    """
    try:
        async with session.get(
            f"{CLOB_API}/midpoint",
            params={"token_id": token_id},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            r.raise_for_status()
            data = await r.json()
            price = float(data.get("mid", 0))
            # For NO side the price is 1 - YES price
            return (1.0 - price) if side == "NO" else price
    except Exception as e:
        logger.error(f"CLOB price fetch error for token {token_id}: {e}")
        return None

def get_token_id(market: dict, side: str) -> Optional[str]:
    """Extract the YES or NO token_id from a Gamma market object."""
    tokens = market.get("tokens") or market.get("outcomePrices", [])
    # tokens is usually a list of dicts: [{outcome: "Yes", token_id: "..."}, ...]
    if isinstance(tokens, list):
        for t in tokens:
            if isinstance(t, dict):
                outcome = (t.get("outcome") or "").upper()
                if outcome == side:
                    return t.get("token_id") or t.get("tokenId")
    # Fallback: some responses store clobTokenIds as a pair [yes_id, no_id]
    clob_ids = market.get("clobTokenIds") or market.get("clob_token_ids", [])
    if isinstance(clob_ids, list) and len(clob_ids) >= 2:
        return clob_ids[0] if side == "YES" else clob_ids[1]
    return None

# ─── Paper Trading Engine ────────────────────────────────────────────────────
def open_position(market: dict, side: str, entry_price: float, amount_usd: float, signal: dict) -> dict:
    global _position_counter
    _position_counter += 1
    pos_id = _position_counter

    # Shares bought = amount / entry_price  (each share pays $1 if correct)
    shares = amount_usd / entry_price if entry_price > 0 else 0

    position = {
        "id": pos_id,
        "market_slug": market.get("slug", "unknown"),
        "market_title": market.get("question") or market.get("title", signal["market_query"]),
        "side": side,
        "entry_price": entry_price,
        "entry_amount_usd": amount_usd,
        "shares": shares,
        "token_id": get_token_id(market, side),
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "current_price": entry_price,
        "unrealized_pnl": 0.0,
        "whale_wallet": signal.get("wallet"),
        "market": market,
    }
    portfolio["positions"][pos_id] = position
    portfolio["balance_usd"] -= amount_usd
    logger.info(f"[PAPER] Opened position #{pos_id}: {side} on '{position['market_title']}' "
                f"@ {entry_price:.3f} | ${amount_usd:.2f} | {shares:.2f} shares")
    
    # Persistent save
    asyncio.create_task(save_trade_to_db(position))
    
    return position

async def update_positions(session: aiohttp.ClientSession):
    """Refresh current prices for all open positions and recalculate PnL."""
    for pos_id, pos in list(portfolio["positions"].items()):
        token_id = pos.get("token_id")
        if not token_id:
            continue
        price = await get_market_price(session, token_id, pos["side"])
        if price is None:
            continue
        pos["current_price"] = price
        pos["unrealized_pnl"] = (price - pos["entry_price"]) * pos["shares"]
    
    # Sync unrealized PnL to DB
    await update_db_pnl()

def portfolio_summary() -> str:
    """Build a human-readable portfolio summary."""
    total_invested = sum(p["entry_amount_usd"] for p in portfolio["positions"].values())
    total_unrealized = sum(p["unrealized_pnl"] for p in portfolio["positions"].values())
    realized = sum(t.get("realized_pnl", 0) for t in portfolio["closed_trades"])
    total_pnl = total_unrealized + realized

    lines = [
        "📊 <b>Paper Portfolio — Polymarket Copy-Trader</b>",
        f"💰 Cash: <b>${portfolio['balance_usd']:,.2f}</b>",
        f"📈 Open Positions: <b>{len(portfolio['positions'])}</b> (${total_invested:,.2f} invested)",
        f"🟢 Unrealized PnL: <b>${total_unrealized:+,.2f}</b>",
        f"✅ Realized PnL: <b>${realized:+,.2f}</b>",
        f"📉 Total PnL: <b>${total_pnl:+,.2f}</b>",
        "",
    ]

    for pos in portfolio["positions"].values():
        arrow = "🟢" if pos["unrealized_pnl"] >= 0 else "🔴"
        lines.append(
            f"{arrow} #{pos['id']} <b>{pos['side']}</b> – {pos['market_title'][:60]}\n"
            f"   Entry: {pos['entry_price']:.3f} → Now: {pos['current_price']:.3f} | "
            f"PnL: <b>${pos['unrealized_pnl']:+,.2f}</b>"
        )

    if portfolio["closed_trades"]:
        lines.append("\n<b>Recent Closed Trades:</b>")
        for t in portfolio["closed_trades"][-5:]:
            emoji = "✅" if t["realized_pnl"] >= 0 else "❌"
            lines.append(f"{emoji} {t['side']} {t['market_title'][:50]} → ${t['realized_pnl']:+,.2f}")

    return "\n".join(lines)

# ─── Main Signal Handler ─────────────────────────────────────────────────────
async def handle_signal(event, session: aiohttp.ClientSession):
    """Process an incoming Telegram message as a potential trading signal."""
    text = event.raw_text or ""
    logger.info(f"New message ({len(text)} chars). Checking for signal...")

    signal = parse_signal(text)
    if not signal:
        logger.info("→ Not a valid signal, skipping.")
        return

    logger.info(f"✅ Signal detected: {signal['side']} on '{signal['market_query']}' "
                f"| Amount: ${signal['amount_usd']:,.0f} | Prob: {signal['probability']}")

    # Resolve market on Polymarket
    market = await resolve_market(session, signal["market_query"])
    if not market:
        await send_telegram(
            session,
            f"⚠️ <b>Signal received but market not found on Polymarket</b>\n"
            f"Query: <i>{signal['market_query']}</i>"
        )
        return

    market_title = market.get("question") or market.get("title", signal["market_query"])
    market_slug  = market.get("slug", "")
    market_url   = f"https://polymarket.com/event/{market_slug}" if market_slug else "https://polymarket.com"

    # Get token ID and current price
    token_id = get_token_id(market, signal["side"])
    if not token_id:
        logger.warning(f"Could not find token_id for {signal['side']} on market: {market_title}")

    entry_price = await get_market_price(session, token_id, signal["side"]) if token_id else signal["probability"]
    if entry_price is None:
        entry_price = signal["probability"] or 0.5

    # Determine paper trade size: mirror the whale's bet scaled to our virtual balance,
    # capped at 10% of balance per trade.
    bet_cap = portfolio["balance_usd"] * 0.10
    if signal["amount_usd"] > 0:
        # Scale proportionally: whale ratio = amount / some "full bet" reference of 50,000
        REFERENCE_WHALE_BET = 50_000
        ratio = min(signal["amount_usd"] / REFERENCE_WHALE_BET, 1.0)
        paper_bet = round(ratio * VIRTUAL_BALANCE * 0.10, 2)
        paper_bet = max(10.0, min(paper_bet, bet_cap))
    else:
        paper_bet = round(bet_cap * 0.25, 2)  # default: 2.5% of balance

    if portfolio["balance_usd"] < paper_bet:
        await send_telegram(
            session,
            f"⚠️ Insufficient paper balance (${portfolio['balance_usd']:.2f}) to copy trade."
        )
        return

    # Open paper position
    pos = open_position(market, signal["side"], entry_price, paper_bet, signal)

    # Send Telegram notification
    whale_info = ""
    if signal.get("wallet"):
        short_wallet = signal["wallet"][:6] + "..." + signal["wallet"][-4:]
        whale_info = f"\n🐋 Whale: <code>{short_wallet}</code> (real: ${signal['amount_usd']:,.0f})"

    prob_str = f"{signal['probability']*100:.0f}%" if signal["probability"] else "N/A"
    msg = (
        f"🚀 <b>Paper Trade Opened</b> #{pos['id']}\n\n"
        f"📌 <b>Market:</b> <a href='{market_url}'>{market_title[:80]}</a>\n"
        f"📍 <b>Side:</b> {signal['side']}\n"
        f"💵 <b>Entry Price:</b> {entry_price:.3f} ({prob_str} prob)\n"
        f"💰 <b>Paper Bet:</b> ${paper_bet:,.2f}\n"
        f"🎯 <b>Potential Payout:</b> ${pos['shares']:.2f}"
        f"{whale_info}\n\n"
        f"💼 Remaining Cash: ${portfolio['balance_usd']:,.2f}"
    )
    await send_telegram(session, msg)

# ─── Hourly PnL Reporter ─────────────────────────────────────────────────────
async def pnl_reporter(session: aiohttp.ClientSession):
    """Background task: refresh positions and send PnL report every hour."""
    while True:
        await asyncio.sleep(PNL_REPORT_INTERVAL)
        try:
            logger.info("⏱ PnL report interval — updating positions...")
            await update_positions(session)
            summary = portfolio_summary()
            await send_telegram(session, summary)
        except Exception as e:
            logger.error(f"PnL reporter error: {e}")

# ─── Entry Point ─────────────────────────────────────────────────────────────
async def main():
    # Validate config
    missing = []
    if not TELEGRAM_API_ID:    missing.append("TELEGRAM_API_ID")
    if not TELEGRAM_API_HASH:  missing.append("TELEGRAM_API_HASH")
    if not SIGNAL_CHANNEL:     missing.append("SIGNAL_CHANNEL")

    if missing:
        logger.error(f"❌ Missing required environment variables: {', '.join(missing)}")
        logger.error("Set them in your .env file and restart.")
        return

    logger.info("=" * 60)
    logger.info("  Polymarket Copy-Trading Bot  [PAPER MODE]")
    logger.info("=" * 60)
    logger.info(f"  Virtual Balance : ${VIRTUAL_BALANCE:,.2f}")
    logger.info(f"  Signal Channel  : {SIGNAL_CHANNEL}")
    logger.info(f"  Report Interval : {PNL_REPORT_INTERVAL // 60} minutes")
    logger.info("=" * 60)

    # Initialize Database
    await init_db()

    # Create shared aiohttp session
    async with aiohttp.ClientSession() as session:
        # Send startup message
        start_msg = (
            f"🤖 <b>Polymarket Copy-Trading Bot Started</b>\n\n"
            f"📡 Listening to: <code>{SIGNAL_CHANNEL}</code>\n"
            f"💰 Virtual Balance: <b>${VIRTUAL_BALANCE:,.2f}</b>\n"
            f"📊 Mode: <b>Paper Trading</b>\n\n"
            f"Ready to copy whale trades on Polymarket! 🐋"
        )
        await send_telegram(session, start_msg)

        # Start Telethon userbot
        client = TelegramClient(
            "copytrade_session",
            TELEGRAM_API_ID,
            TELEGRAM_API_HASH,
        )

        @client.on(events.NewMessage(chats=SIGNAL_CHANNEL))
        async def _handler(event):
            try:
                await handle_signal(event, session)
            except Exception as e:
                logger.error(f"Handler error: {e}")

        await client.start()
        logger.info(f"✅ Telethon connected. Listening for signals on: {SIGNAL_CHANNEL}")

        # Kick off background PnL reporter
        asyncio.create_task(pnl_reporter(session))

        # Run until disconnected
        await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
