import os
import json
import asyncio
import logging
import requests
import asyncpg
import websockets
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
AGENT_ID = "MoneyPigBot"
POLYMARKETSCAN_API_URL = "https://gzydspfquuaudqeztorw.supabase.co/functions/v1/agent-api"

# Гаманці китів для відстеження (встав сюди реальні адреси)
WHALE_WALLETS = [
    "0x492442eab586f242b53bda933fd5de859c8a3782",
    "0xf6d91fbbefacade7d5908eb13e16acf1efeb305e",
    "0x2a2c53bd278c04da9962fcf96490e17f3dfb9bc1",
    "0x0b9cae2b0dfe7a71c413e0604eaac1c352f87e44",
    "0x07b8e44b90cc3e91b8d5fe60ea810d2534638e25",
    "0x72b40c0012682ef52228ad53ef955f9e4f177d67",
    "0x86cd93526a4e7ad201ed3d1c6f2647b61837504c",
    "0x78ad03d27582cd1b5255d364ff8f093b6cf745cc"
]

# Завантажуємо змінні середовища
load_dotenv()

PRIVATE_KEY = os.getenv("POLYGON_PRIVATE_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
QUICKNODE_WSS_URL = os.getenv("QUICKNODE_WSS_URL")

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

async def listen_to_whales_ws():
    """Слухає QuickNode WebSockets для швидкої реакції на події китів (без поллінгу)."""
    if not QUICKNODE_WSS_URL or not QUICKNODE_WSS_URL.startswith("wss://"):
        logger.error("QUICKNODE_WSS_URL не налаштований. WebSocket трекер вимкнено.")
        return

    logger.info(f"🔗 Підключення до QuickNode WebSocket...")
    
    # Форматуємо гаманці для Topics (ERC1155 TransferSingle `to` або `from` fields, доповнені до 32 байт)
    # Зверніть увагу: це базовий приклад. У реальному житті краще фільтрувати всі логи Polymarket Exchange
    # або парсити pending transactions (mempool).
    whale_topics = ["0x000000000000000000000000" + w[2:].lower() for w in WHALE_WALLETS]
    
    subscription_payload = {
        "id": 1,
        "jsonrpc": "2.0",
        "method": "eth_subscribe",
        "params": [
            "logs",
            {
                # Трансфери на Polymarket Exchange / CTF contract
                # Якщо залишити пустим, слухатиме весь блокчейн за участю цих топіків (гаманців)
                "topics": [None, None, whale_topics] # Фільтр: гаманець є 'to' (Topic 2 у TransferSingle)
            }
        ]
    }

    while True:
        try:
            async with websockets.connect(QUICKNODE_WSS_URL) as ws:
                await ws.send(json.dumps(subscription_payload))
                response = await ws.recv()
                logger.info(f"✅ Успішна підписка на події китів! ID: {json.loads(response).get('result')}")

                while True:
                    msg = await ws.recv()
                    data = json.loads(msg)
                    
                    if "params" in data and "result" in data["params"]:
                        log_data = data["params"]["result"]
                        tx_hash = log_data.get("transactionHash")
                        logger.info(f"🐋 Виявлено активність кита! Tx: {tx_hash}")
                        
                        # Коли трапилась подія — обробляємо її (отримуємо ринок, перевіряємо AI тощо)
                        # Для демо ми запускаємо fetch_opportunities як реакцію на подію, а не по таймеру
                        asyncio.create_task(process_opportunities_on_event())
                        
        except Exception as e:
            logger.error(f"WebSocket відключився ({e}). Реконект через 5 сек...")
            await asyncio.sleep(5)

async def process_opportunities_on_event():
    """Викликається коли WebSocket фіксує рух. Аналізує ринки і приймає рішення."""
    try:
        opps = await fetch_opportunities()
        for opp in opps:
            title = opp.get("title", "Unknown")
            pm_price = float(opp.get("polymarketPrice", 0))
            ai_price = float(opp.get("aiConsensus", 0))
            direction = opp.get("divergenceDirection", "unknown")
            
            logger.info(f"💡 Знайдено оппортьюніті після активності: '{title}' | Поточна: {pm_price} | AI: {ai_price} ({direction})")
            
            ai_decision = await validate_trade_with_ai(title, pm_price, ai_price, direction)
            confidence = ai_decision.get("confidence", 0)
            should_trade = ai_decision.get("trade", False)
            
            if should_trade and confidence >= 70:
                logger.info("🚀 Угода підтверджена! Виконання...")
                await execute_trade(opp, ai_decision, TRADE_AMOUNT_USD)
            else:
                logger.info("⛔ Відхилено ШІ.")
                
    except Exception as e:
        logger.error(f"Помилка під час обробки події: {e}")

async def main_loop():
    logger.info("=== Запуск Polymarket AI Bot (MoneyPigBot) ===")
    logger.info(f"Режим: {'SIMULATION (Arena)' if SIMULATION_MODE else 'LIVE TRADING'}")
    logger.info(f"Баланс на ставку: ${TRADE_AMOUNT_USD}")
    
    await init_db()
    await check_arena_portfolio()
    
    logger.info("📡 Запуск WebSocket слухача...")
    # Запускаємо WebSockets listener замість while True з sleep(60)
    await listen_to_whales_ws()

if __name__ == "__main__":
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        logger.info("Бота зупинено користувачем.")
