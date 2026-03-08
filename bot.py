import os
import time
import json
import asyncio
import logging
import requests
import asyncpg
from typing import List, Dict, Any, Optional
from dotenv import load_dotenv

from google import genai
from google.genai import types
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType

# Налаштування логування
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- ВАЖЛИВІ НАЛАШТУВАННЯ ---
SIMULATION_MODE = True  # True = Paper Trading на Agent Arena, False = Реальні угоди
TRADE_AMOUNT_USD = 2.5   # $2-$3 як вимагається для банку $100
MAX_SLIPPAGE_PCT = 0.03  # 3% максимальне прослизання
CHECK_INTERVAL_SEC = 60  # Інтервал перевірки Арени
AGENT_ID = "MoneyPigBot"
POLYMARKETSCAN_API_URL = "https://gzydspfquuaudqeztorw.supabase.co/functions/v1/agent-api"

# Завантажуємо змінні середовища
load_dotenv()

PRIVATE_KEY = os.getenv("POLYGON_PRIVATE_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

# Ініціалізація пулу бази даних
db_pool = None

async def init_db():
    global db_pool
    if not DATABASE_URL:
        logger.info("DATABASE_URL не знайдено, пропускаємо ініціалізацію БД.")
        return
    try:
        db_pool = await asyncpg.create_pool(DATABASE_URL)
        async with db_pool.acquire() as conn:
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS trades (
                    id SERIAL PRIMARY KEY,
                    market_slug VARCHAR(255),
                    market_id VARCHAR(255),
                    side VARCHAR(10),
                    execution_price FLOAT,
                    amount_usd FLOAT,
                    fair_value FLOAT,
                    confidence INT,
                    reason TEXT,
                    is_simulation BOOLEAN,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
        logger.info("✅ База даних PostgreSQL успішно ініціалізована (таблиця trades).")
    except Exception as e:
        logger.error(f"❌ Помилка підключення до БД: {e}")

# Ініціалізація клієнта Gemini
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

def init_clob_client() -> Optional[ClobClient]:
    """Ініціалізація офіційного клієнта Polymarket (py-clob-client)."""
    if not PRIVATE_KEY:
        logger.warning("ГЕНЕРАЦІЯ РЕАЛЬНИХ ОРДЕРІВ НЕМОЖЛИВА: POLYGON_PRIVATE_KEY не знайдений.")
        return None
    try:
        # Host та Chain ID для Polygon Mainnet
        client = ClobClient(
            host="https://clob.polymarket.com",
            key=PRIVATE_KEY,
            chain_id=137
        )
        client.set_api_creds(client.create_or_derive_api_creds())
        return client
    except Exception as e:
        logger.error(f"Помилка ініціалізації ClobClient: {e}")
        return None

clob_client = init_clob_client() if not SIMULATION_MODE else None

async def fetch_opportunities() -> List[Dict[str, Any]]:
    """
    Отримує ринки з найбільшою розбіжністю (AI vs Humans) через API PolymarketScan.
    """
    try:
        url = f"{POLYMARKETSCAN_API_URL}?action=ai-vs-humans&limit=5"
        response = await asyncio.to_thread(requests.get, url, timeout=10)
        response.raise_for_status()
        data = response.json().get("data", [])
        return data
    except Exception as e:
        logger.error(f"Помилка отримання AI vs Humans opportunities: {e}")
        return []

async def fetch_market_details(slug: str) -> Optional[Dict[str, Any]]:
    """
    Отримує детальні дані ринку, щоб дізнатися Market ID (Condition ID).
    """
    try:
        url = f"{POLYMARKETSCAN_API_URL}?action=market&slug={slug}"
        response = await asyncio.to_thread(requests.get, url, timeout=10)
        response.raise_for_status()
        return response.json().get("data")
    except Exception as e:
        logger.error(f"Помилка отримання деталей ринку {slug}: {e}")
        return None

async def validate_trade_with_ai(market_title: str, current_price: float, ai_consensus: float, direction: str) -> Dict[str, Any]:
    """
    Оцінює розбіжність за допомогою LLM.
    """
    if not gemini_client:
        logger.error("Gemini API Key не налаштований. Пропускаємо перевірку AI.")
        return {"confidence": 0, "trade": False, "reason": "No API Key", "fair_value": ai_consensus}

    system_prompt = (
        "Ти — елітний Quant Researcher та аналітик ринків прогнозів Polymarket. "
        "Твоє завдання — аналізувати розбіжності між прогнозом AI та поточною Polymarket ціною (Humans). "
        "Будь консервативним. Якщо розбіжність виглядає як помилка чи шум на хайповому ринку, відхиляй угоду. "
        "Відповідай виключно у форматі JSON. "
        "Схема JSON: {\"confidence\": int (від 0 до 100), \"trade\": bool, \"reason\": str, \"fair_value\": float}."
    )
    
    user_prompt = (
        f"Ринок: '{market_title}'\n"
        f"Поточна ціна на Polymarket: {current_price}$\n"
        f"Незалежний консенсус AI-моделей: {ai_consensus}$\n"
        f"Напрямок розбіжності (Divergence Direction): {direction}\n"
        f"Запитуємо твій висновок: чи є тут математична перевага (edge) для ставки у напрямку {direction}? "
        f"Також поверни fair_value (твою впевненість у відсотках ймовірності, наприклад 0.75)."
    )

    try:
        response = await gemini_client.aio.models.generate_content(
            model="gemini-2.5-flash",
            contents=user_prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                temperature=0.2, 
                response_mime_type="application/json",
            )
        )
        
        result_str = response.text
        if result_str:
            result_json = json.loads(result_str)
            if "fair_value" not in result_json:
                 result_json["fair_value"] = ai_consensus
            return result_json
        else:
            return {"confidence": 0, "trade": False, "reason": "Empty AI response", "fair_value": ai_consensus}
        
    except json.JSONDecodeError as e:
        logger.error(f"Помилка парсингу JSON від AI: {e}")
    except Exception as e:
        logger.error(f"Помилка виклику AI: {e}")
        
    return {"confidence": 0, "trade": False, "reason": "System Error", "fair_value": ai_consensus}

async def execute_trade(opportunity: Dict[str, Any], ai_decision: Dict[str, Any], amount_usd: float):
    """
    Реєструє угоду.
    В SIMULATION_MODE відправляє в Agent Arena API.
    В LIVE (SIMULATION_MODE=False) - купує через py-clob-client.
    """
    market_slug = opportunity.get("slug")
    if not market_slug:
         logger.warning("Оппортьюніті не містить slug.")
         return
         
    # Отримуємо додаткові деталі ринку для market_id
    market_details = await fetch_market_details(market_slug)
    market_id = market_details.get("market_id") if market_details else market_slug # Fallback до slug якщо щось піде не так
    
    direction = opportunity.get("divergenceDirection", "bullish")
    side = "YES" if direction.lower() == "bullish" else "NO"
    market_price = float(opportunity.get("polymarketPrice", 0.5))
    fair_value = ai_decision.get("fair_value", 0.5)

    if SIMULATION_MODE:
        logger.info(f"[PAPER TRADE / ARENA] Відправка: {side} на {market_slug} | "
                    f"Ціна: ${market_price} | Сума: ${amount_usd} | Fair Value: {fair_value}")
        try:
            payload = {
                "agent_id": AGENT_ID,
                "market_id": market_id,
                "side": side,
                "amount": amount_usd,
                "action": "BUY",
                "fair_value": fair_value
            }
            url = f"{POLYMARKETSCAN_API_URL}?action=place_order"
            response = await asyncio.to_thread(requests.post, url, json=payload, timeout=10)
            result = response.json()
            if result.get("ok"):
                logger.info(f"✅ [ARENA SUCCESS] Угода прийнята симулятором: {result.get('data')}")
            else:
                logger.warning(f"❌ [ARENA ERROR] Відхилено Ареною: {result}")
        except Exception as e:
            logger.error(f"❌ [ARENA NETWORK ERROR] {e}")
        return

    # Реальна торгівля на Polygon
    try:
        if not clob_client:
            raise ValueError("ClobClient не ініціалізовано!")

        max_price = round(market_price * (1.0 + MAX_SLIPPAGE_PCT), 3)
        if max_price > 0.99: max_price = 0.99
        size = round(amount_usd / max_price, 2)
             
        # Увага: Для реальної торгівлі потрібен CLOB Token ID (специфічний для YES/NO). 
        # API PolymarketScan повертає його масивом у market_details. 
        # Якщо немає - реальна торгівля впаде.
        clob_tokens = market_details.get("clobTokenIds", []) if market_details else []
        if not clob_tokens or len(clob_tokens) < 2:
            logger.error(f"Не знайдено ClobToken Ids для ринку {market_slug}. Trade cancelled.")
            return
            
        token_id = clob_tokens[0] if side == "YES" else clob_tokens[1]

        logger.info(f"[LIVE TRADE] Купую {side} (Token: {token_id}) по {max_price} на суму ${amount_usd}")

        order_args = OrderArgs(
            price=max_price,
            size=size,
            side="BUY",
            token_id=token_id
        )

        signed_order = clob_client.create_order(order_args)
        resp = clob_client.post_order(signed_order, order_type=OrderType.FOK)
        
        if resp and resp.get("success"):
            logger.info(f"✅ [SUCCESS] Ордер виконано! ID: {resp.get('orderID')}")
        else:
            logger.warning(f"⚠️ [FAILED] Відхилено Polymarket: {resp.get('errorMsg', resp)}")

    except Exception as e:
        logger.error(f"[ERROR] Помилка виконання Live-угоди: {e}")

    # Після успішного (або симульованого) виконання - запишемо в PostgreSQL (якщо доступно)
    if db_pool:
        try:
            # Для симуляції ціна виконання = market_price, для лайву = max_price
            exec_price = market_price if SIMULATION_MODE else max_price
            async with db_pool.acquire() as conn:
                await conn.execute('''
                    INSERT INTO trades 
                    (market_slug, market_id, side, execution_price, amount_usd, fair_value, confidence, reason, is_simulation)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                ''', market_slug, market_id, side, exec_price, amount_usd, fair_value, ai_decision.get("confidence", 0), ai_decision.get("reason", ""), SIMULATION_MODE)
        except Exception as db_e:
            logger.error(f"Помилка запису в БД: {db_e}")

async def check_arena_portfolio():
    """Перевіряємо поточний баланс Агента в Арені."""
    if SIMULATION_MODE:
        try:
            url = f"{POLYMARKETSCAN_API_URL}?action=my_portfolio&agent_id={AGENT_ID}"
            response = await asyncio.to_thread(requests.get, url, timeout=10)
            data = response.json().get("data", {})
            value = data.get("portfolio_value", 0)
            logger.info(f"📊 Поточний Portfolio Value в Arena: ${value}")
        except:
             pass

async def main_loop():
    logger.info("=== Запуск Polymarket AI Bot (MoneyPigBot) ===")
    logger.info(f"Режим: {'SIMULATION (Arena)' if SIMULATION_MODE else 'LIVE TRADING'}")
    logger.info(f"Баланс на ставку: ${TRADE_AMOUNT_USD}")
    
    await init_db()
    
    processed_slugs = set()

    while True:
        try:
            await check_arena_portfolio()
            logger.info("📡 Шукаємо нові можливості AI vs Humans...")
            
            opps = await fetch_opportunities()
            
            for opp in opps:
                slug = opp.get("slug")
                if slug in processed_slugs:
                    continue 
                    
                processed_slugs.add(slug)
                
                title = opp.get("title", "Unknown")
                pm_price = float(opp.get("polymarketPrice", 0))
                ai_price = float(opp.get("aiConsensus", 0))
                direction = opp.get("divergenceDirection", "unknown")
                
                logger.info(f"💡 Знайдено сигнал: '{title}' | Polymarket ціна: {pm_price} | AI: {ai_price} ({direction})")
                
                # Запускаємо власний Gemini Filter
                ai_decision = await validate_trade_with_ai(title, pm_price, ai_price, direction)
                
                confidence = ai_decision.get("confidence", 0)
                should_trade = ai_decision.get("trade", False)
                reason = ai_decision.get("reason", "Не вказано")
                
                logger.info(f"🤖 Gemini Аналіз: Confidence: {confidence}%, Trade: {should_trade}. Reason: {reason}")
                
                if should_trade and confidence >= 70:
                    logger.info("🚀 Угода підтверджена! Виконання...")
                    await execute_trade(opp, ai_decision, TRADE_AMOUNT_USD)
                else:
                    logger.info("⛔ Угоду відхилено ризик-менеджером Gemini.")
                    
        except Exception as e:
            logger.error(f"Непередбачена помилка в основному циклі: {e}")
            
        logger.info(f"Очікування {CHECK_INTERVAL_SEC} секунд...")
        await asyncio.sleep(CHECK_INTERVAL_SEC)

if __name__ == "__main__":
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        logger.info("Бота зупинено користувачем.")
