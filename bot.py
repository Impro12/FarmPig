"""
Polymarket Copy-Trading Bot
===========================
Listens to trading signals in a Telegram channel and replicates them as 
virtual paper trades on Polymarket with PostgreSQL persistence and TG reporting.
"""

import os
import re
import asyncio
import logging
import aiohttp
import asyncpg
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from dotenv import load_dotenv
from telethon import TelegramClient, events

# ─── Configuration ──────────────────────────────────────────────────────────
load_dotenv()

# Required env variables
TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID") or "0")
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
SIGNAL_CHANNEL = os.getenv("SIGNAL_CHANNEL", "")
DATABASE_URL = os.getenv("DATABASE_URL")
VIRTUAL_BALANCE = float(os.getenv("VIRTUAL_BALANCE", "10000"))

# Constants
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
REPORT_INTERVAL = 3600  # 1 hour

# ─── Logging Setup ──────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("MoneyPigBot")

class MoneyPigBot:
    def __init__(self):
        self.portfolio = {
            "cash": VIRTUAL_BALANCE,
            "positions": {},
            "closed": [],
        }
        self.db_pool = None
        self.http_session = None
        self.tg_client = None
        self._pos_counter = 0

    # ─── Database methods ────────────────────────────────────────────────────
    async def init_db(self):
        if not DATABASE_URL:
            logger.warning("No DATABASE_URL found. Running in memory-only mode.")
            return

        try:
            self.db_pool = await asyncpg.create_pool(DATABASE_URL)
            async with self.db_pool.acquire() as conn:
                await conn.execute('''
                    CREATE TABLE IF NOT EXISTS trades (
                        id SERIAL PRIMARY KEY,
                        market_slug TEXT,
                        market_title TEXT,
                        side TEXT,
                        entry_price FLOAT,
                        amount_usd FLOAT,
                        shares FLOAT,
                        token_id TEXT,
                        status TEXT DEFAULT 'OPEN',
                        pnl FLOAT DEFAULT 0.0,
                        opened_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                await conn.execute("CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT)")
                
                # Restore balance and positions
                val = await conn.fetchval("SELECT value FROM state WHERE key='balance'")
                if val: self.portfolio["cash"] = float(val)

                rows = await conn.fetch("SELECT * FROM trades WHERE status='OPEN'")
                for r in rows:
                    p_id = r['id']
                    self.portfolio["positions"][p_id] = {
                        "id": p_id, "slug": r['market_slug'], "title": r['market_title'],
                        "side": r['side'], "entry": r['entry_price'], "invested": r['amount_usd'],
                        "shares": r['shares'], "token_id": r['token_id'], "pnl": r['pnl'],
                        "cur_price": r['entry_price']
                    }
                    self._pos_counter = max(self._pos_counter, p_id)
            logger.info("✅ Database connected and state restored.")
        except Exception as e:
            logger.error(f"❌ DB Init error: {e}")

    async def save_trade(self, pos: Dict[str, Any]):
        if not self.db_pool: return
        async with self.db_pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO trades (market_slug, market_title, side, entry_price, amount_usd, shares, token_id)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
            ''', pos['slug'], pos['title'], pos['side'], pos['entry'], pos['invested'], pos['shares'], pos['token_id'])
            await conn.execute("UPDATE state SET value=$1 WHERE key='balance'", str(self.portfolio['cash']))

    # ─── API Helpers ─────────────────────────────────────────────────────────
    async def notify(self, text: str):
        if not self.http_session: return
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        try:
            async with self.http_session.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}) as r:
                if r.status != 200: logger.error(f"TG notify failed: {r.status}")
        except Exception as e: logger.error(f"TG notify error: {e}")

    async def get_market(self, query: str) -> Optional[Dict]:
        try:
            async with self.http_session.get(f"{GAMMA_API}/markets", params={"q": query, "active": "true"}) as r:
                data = await r.json()
                return data[0] if data else None
        except Exception as e: logger.error(f"Gamma API error: {e}"); return None

    async def get_price(self, token_id: str, side: str) -> Optional[float]:
        try:
            async with self.http_session.get(f"{CLOB_API}/midpoint", params={"token_id": token_id}) as r:
                data = await r.json()
                price = float(data.get("mid", 0))
                return (1.0 - price) if side == "NO" else price
        except Exception: return None

    # ─── Core Logic ──────────────────────────────────────────────────────────
    def parse_signal(self, text: str) -> Optional[Dict]:
        if "polymarket" not in text.lower(): return None
        try:
            # Side: look for "Yes" or "No" before "outcome"
            side_m = re.search(r'"(Yes|No)"\s+outcome', text, re.I)
            side = side_m.group(1).upper() if side_m else ("YES" if "Yes" in text else "NO")
            
            # Title: text in quotes near "for" or "market"
            title_m = re.search(r'(?:for|market)\s+"([^"]{5,})"', text, re.I)
            if not title_m: title_m = re.search(r'"([^"]{10,})"', text)
            title = title_m.group(1) if title_m else None
            
            # Amount: $10k, $1.5M, etc.
            amt_m = re.search(r'\$([0-9,.]+)\s*([kKmMBb]?)', text)
            if not amt_m or not title: return None
            
            val = float(amt_m.group(1).replace(",", ""))
            suffix = amt_m.group(2).upper()
            mult = {"K": 1000, "M": 1_000_000, "B": 1_000_000_000}.get(suffix, 1)
            
            return {"query": title, "side": side, "whale_amt": val * mult}
        except Exception as e:
            logger.debug(f"Parse error: {e}")
            return None

    async def process_msg(self, event):
        sig = self.parse_signal(event.raw_text)
        if not sig: return
        
        logger.info(f"Signal: {sig['side']} on '{sig['query']}' (${sig['whale_amt']:,.0f})")
        m = await self.get_market(sig['query'])
        if not m: await self.notify(f"⚠️ Market not found: {sig['query']}"); return

        t_id_key = "tokenId" if "tokenId" in m.get("tokens", [{}])[0] else "token_id"
        t_id = next((t[t_id_key] for t in m['tokens'] if t['outcome'].upper() == sig['side']), None)
        price = await self.get_price(t_id, sig['side']) or 0.5

        # Paper trade execution (capped at 10% of cash)
        bet = min(self.portfolio['cash'] * 0.1, 500.0)
        self.portfolio['cash'] -= bet
        self._pos_counter += 1
        pos = {
            "id": self._pos_counter, "slug": m['slug'], "title": m.get('question', sig['query']),
            "side": sig['side'], "entry": price, "invested": bet, "shares": bet/price,
            "token_id": t_id, "pnl": 0.0, "cur_price": price
        }
        self.portfolio['positions'][pos['id']] = pos
        await self.save_trade(pos)
        
        await self.notify(
            f"🚀 <b>Copy-Trade Opened</b>\n\n"
            f"📌 {pos['title']}\n"
            f"📍 Side: {pos['side']} | Entry: {pos['entry']:.3f}\n"
            f"💰 Bet: ${bet:.2f} | Balance: ${self.portfolio['cash']:,.2f}"
        )

    async def report_loop(self):
        while True:
            await asyncio.sleep(REPORT_INTERVAL)
            summary = [f"📊 <b>Portfolio Summary</b>\nCash: ${self.portfolio['cash']:,.2f}\nOpen: {len(self.portfolio['positions'])}"]
            for p in self.portfolio['positions'].values():
                summary.append(f"• {p['side']} {p['title'][:40]}..: {p['pnl']:+,.2f}")
            await self.notify("\n".join(summary))

    async def start(self):
        await self.init_db()
        self.http_session = aiohttp.ClientSession()
        self.tg_client = TelegramClient("copytrade_session", TELEGRAM_API_ID, TELEGRAM_API_HASH)
        
        @self.tg_client.on(events.NewMessage(chats=SIGNAL_CHANNEL))
        async def handler(event): await self.process_msg(event)

        await self.tg_client.start()
        asyncio.create_task(self.report_loop())
        logger.info(f"✅ Bot started. Listening on {SIGNAL_CHANNEL}")
        await self.tg_client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(MoneyPigBot().start())
