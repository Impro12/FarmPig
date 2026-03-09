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
TOTAL_BANKROLL_USD = 100.0   # Твій загальний банк для реальних угод (в USD)
MAX_SLIPPAGE_PCT = 0.03  # 3% максимальне прослизання
KELLY_MULTIPLIER = 0.25  # Quarter-Kelly (консервативний підхід)
MAX_BET_PERCENTAGE = 0.05 # Ніколи не ставити більше 5% від банку (Ризик-менеджмент)
AGENT_ID = "MoneyPigBot"
POLYMARKETSCAN_API_URL = "https://gzydspfquuaudqeztorw.supabase.co/functions/v1/agent-api"

# Гаманці китів для відстеження (буде оновлюватися динамічно)
WHALE_WALLETS = set()
PROCESSED_SLUGS = set() # Зберігаємо slug ринків, де ми вже відторгували
WHALE_DISCOVERY_THRESHOLD_USD = 10000  # Фікс: мінімальний розмыр транзакції щоб вважати китом, можна збільшувати
CTF_EXCHANGE_ADDRESS = "0x4bFB41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"  # Контракт Polymarket CTF

# Кеш рішень ШІ (щоб не питати Gemini про той самий ринок занадто часто)
# Формат: {slug: {"decision": dict, "timestamp": float}}
AI_DECISION_CACHE = {}
AI_CACHE_TTL = 3600  # 1 година (в секундах)

# Завантажуємо змінні середовища
load_dotenv()

PRIVATE_KEY = os.getenv("POLYGON_PRIVATE_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
QUICKNODE_WSS_URL = os.getenv("QUICKNODE_WSS_URL")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Обмежувач швидкості (Rate Limiter) - 5 запитів на ХВИЛИНУ
# 60 сек / 5 = 12 секунд між запитами
async def rate_limited_ai_call():
    """Забезпечує паузу 12 секунд для дотримання ліміту 5 RPM"""
    logger.info("⏳ Очікування 12с для дотримання ліміту 5 RPM...")
    await asyncio.sleep(12) 

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

async def load_processed_slugs():
    """Завантажує вже відпрацьовані ринки з бази даних, щоб не дублювати угоди після перезапуску."""
    global PROCESSED_SLUGS
    if not db_pool:
        return
    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch("SELECT DISTINCT market_slug FROM trades")
            for row in rows:
                PROCESSED_SLUGS.add(row['market_slug'])
        logger.info(f"📁 Завантажено {len(PROCESSED_SLUGS)} відпрацьованих ринків з БД.")
    except Exception as e:
        logger.error(f"Помилка завантаження processed_slugs: {e}")

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

async def send_telegram_message(text: str):
    """Надсилає повідомлення у Telegram"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
        await asyncio.to_thread(requests.post, url, json=payload, timeout=5)
    except Exception as e:
        logger.error(f"Помилка відправки в Telegram: {e}")

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
            model="gemini-2.0-flash",
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

def calculate_optimal_bet(fair_value: float, current_price: float, current_bankroll: float) -> float:
    """Обчислює розмір ставки за адаптованим критерієм Келлі (Quarter-Kelly)"""
    if current_bankroll <= 0:
        return 0.0
        
    edge = fair_value - current_price
    
    # Якщо переваги (edge) немає або вона від'ємна — не торгуємо взагалі
    if edge <= 0:
        return 0.0
        
    # Формула Келлі для бінарних опціонів: f* = (P - C) / (1 - C)
    # Де C - поточна ціна (ямости ставки), P - наша впевненість.
    # Так як тут "ставка" = 1, прибуток = 1 - C
    if current_price >= 1.0: return 0.0 # Захист від ділення на 0
    
    kelly_fraction = edge / (1.0 - current_price)
    
    # Регулювання ризику (Quarter Kelly)
    adj_kelly_fraction = kelly_fraction * KELLY_MULTIPLIER
    
    # Обмеження максимуму
    safest_fraction = min(adj_kelly_fraction, MAX_BET_PERCENTAGE)
    if safest_fraction <= 0: return 0.0
    
    bet_amount = current_bankroll * safest_fraction
    return round(bet_amount, 2)

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

# Глобальна змінна для віртуального балансу Арени
current_arena_portfolio_value = 1000.0

async def check_arena_portfolio():
    """Перевіряємо поточний баланс Агента в Арені."""
    global current_arena_portfolio_value
    if SIMULATION_MODE:
        try:
            url = f"{POLYMARKETSCAN_API_URL}?action=my_portfolio&agent_id={AGENT_ID}"
            response = await asyncio.to_thread(requests.get, url, timeout=10)
            data = response.json().get("data", {})
            value = data.get("portfolio_value", 0)
            if value > 0:
                current_arena_portfolio_value = value
            logger.info(f"📊 Поточний Portfolio Value в Arena: ${current_arena_portfolio_value}")
        except:
             pass

async def fetch_initial_whales():
    """Завантажує початковий список китів з API на старті бота"""
    try:
        url = f"{POLYMARKETSCAN_API_URL}?action=whales&limit=20"
        logger.info(f"Завантаження початкових китів з: {url}")
        
        # Асинхронно робимо HTTP запит
        response = await asyncio.to_thread(requests.get, url, timeout=10)
        data = response.json().get("data", [])
        
        for w in data:
            wallet = w.get("wallet")
            if wallet:
                WHALE_WALLETS.add(wallet.lower())
                
        logger.info(f"🐳 Успішно завантажено {len(WHALE_WALLETS)} китів для відстеження.")
    except Exception as e:
        logger.error(f"Помилка завантаження первинних китів: {e}")

async def listen_to_whales_ws():
    """Слухає QuickNode WebSockets для швидкої реакції на події китів (без поллінгу)."""
    if not QUICKNODE_WSS_URL or not QUICKNODE_WSS_URL.startswith("wss://"):
        logger.error("QUICKNODE_WSS_URL не налаштований. WebSocket трекер вимкнено. Працюватиме лише Автономний сканер.")
        while True:
            await asyncio.sleep(3600)
        return

    logger.info(f"🔗 Підключення до QuickNode WebSocket...")
    
    # Підписуємось на TransferSingle (Erc1155) події контракту Polymarket
    # topic0 для `TransferSingle(address operator, address from, address to, uint256 id, uint256 value)`
    # Це: 0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62
    transfer_single_topic = "0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62"
    
    subscription_payload = {
        "id": 1,
        "jsonrpc": "2.0",
        "method": "eth_subscribe",
        "params": [
            "logs",
            {
                "address": CTF_EXCHANGE_ADDRESS, # Тільки логи з головного контракту Polymarket
                "topics": [transfer_single_topic] 
            }
        ]
    }

    while True:
        try:
            async with websockets.connect(QUICKNODE_WSS_URL) as ws:
                await ws.send(json.dumps(subscription_payload))
                response = await ws.recv()
                logger.info(f"✅ Успішна підписка на CTF Exchange! Шукаю китів та їхні рухи... ID: {json.loads(response).get('result')}")

                while True:
                    msg = await ws.recv()
                    data = json.loads(msg)
                    
                    if "params" in data and "result" in data["params"]:
                        log_data = data["params"]["result"]
                        topics = log_data.get("topics", [])
                        
                        if len(topics) >= 4:
                            # Парсимо TransferSingle: operator, from, to знаходяться в topics[1], topics[2], topics[3] відповідно
                            # Адреса покупця ("to")
                            to_address = "0x" + topics[3][26:].lower()
                            tx_hash = log_data.get("transactionHash")
                            
                            # Перевіряємо, чи ми знаємо цього гаманця
                            if to_address in WHALE_WALLETS:
                                logger.info(f"🐋 Знайомий Кит діє! Tx: {tx_hash} | {to_address}")
                                asyncio.create_task(process_opportunities_on_event())
                            else:
                                # Data містить [id, value] без індексів (розділено на 2 юінти по 32 байти)
                                unindexed_data = log_data.get("data", "0x")[2:]
                                if len(unindexed_data) >= 128:
                                    value_hex = unindexed_data[64:128] # Друга частина
                                    try:
                                        value_int = int(value_hex, 16)
                                        # Тут дуже спрощено: value_int / 1e6 (якщо це USDC shares). 
                                        usd_estimate = value_int / 10**6 
                                        
                                        if usd_estimate >= WHALE_DISCOVERY_THRESHOLD_USD:
                                            logger.info(f"🚨🚨 НОВИЙ КИТ ВИЯВЛЕНИЙ! 🚨🚨")
                                            logger.info(f"Гаманець: {to_address} | Об'єм: ~${usd_estimate:,.2f} | Tx: {tx_hash}")
                                            WHALE_WALLETS.add(to_address)
                                            await send_telegram_message(f"🚨 <b>НОВИЙ КИТ ВИЯВЛЕНИЙ!</b> 🚨\nГаманець: <code>{to_address}</code>\nОб'єм угоди: <b>~${usd_estimate:,.2f}</b>\nTx: <code>{tx_hash}</code>")
                                            # Ми знайшли нового кита, значить він щойно купив, давайте аналізувати
                                            asyncio.create_task(process_opportunities_on_event())
                                    except Exception as parse_e:
                                        pass
                                
        except Exception as e:
            logger.error(f"WebSocket відключився ({e}). Реконект через 5 сек...")
            await asyncio.sleep(5)

async def process_opportunities_on_event(source="WHALE"):
    """Викликається коли WebSocket фіксує рух або по таймеру. Аналізує ринки і приймає рішення."""
    try:
        opps = await fetch_opportunities()
        for opp in opps:
            slug = opp.get("slug")
            if not slug or slug in PROCESSED_SLUGS:
                continue

            title = opp.get("title", "Unknown")
            pm_price = float(opp.get("polymarketPrice", 0))
            ai_price = float(opp.get("aiConsensus", 0))
            direction = opp.get("divergenceDirection", "unknown")
            
            # --- ОПТИМІЗАЦІЯ КВОТИ ---
            # Якщо розбіжність занадто мала (< 3%), ми навіть не питаємо ШІ, щоб економити ліміти API.
            edge = abs(pm_price - ai_price)
            if edge < 0.03:
                logger.info(f"⏭️ [{source}] Скіп: '{title}' (Замалий Edge: {edge*100:.1f}%)")
                continue
                
            logger.info(f"💡 [{source}] Знайдено оппортьюніті: '{title}' | PM: {pm_price} | AI: {ai_price} ({direction})")
            
            # --- ПЕРЕВІРКА КЕШУ ШІ ---
            now = asyncio.get_event_loop().time()
            if slug in AI_DECISION_CACHE:
                cached = AI_DECISION_CACHE[slug]
                if now - cached["timestamp"] < AI_CACHE_TTL:
                    logger.info(f"💾 [{source}] Використовуємо кешоване рішення ШІ для: '{title}'")
                    ai_decision = cached["decision"]
                else:
                    # Час кешу вийшов, робимо новий запит
                    await rate_limited_ai_call()
                    ai_decision = await validate_trade_with_ai(title, pm_price, ai_price, direction)
                    AI_DECISION_CACHE[slug] = {"decision": ai_decision, "timestamp": now}
            else:
                # Новий ринок, робимо запит
                await rate_limited_ai_call()
                ai_decision = await validate_trade_with_ai(title, pm_price, ai_price, direction)
                AI_DECISION_CACHE[slug] = {"decision": ai_decision, "timestamp": now}
            
            confidence = ai_decision.get("confidence", 0)
            should_trade = ai_decision.get("trade", False)
            fair_value = ai_decision.get("fair_value", ai_price)
            
            if should_trade and confidence >= 70:
                # Дані для сайзунга
                bankroll = current_arena_portfolio_value if SIMULATION_MODE else TOTAL_BANKROLL_USD
                optimal_bet = calculate_optimal_bet(fair_value, pm_price, bankroll)
                
                logger.info(f"📐 Розрахований Kelly Size: ${optimal_bet} (з банку ${bankroll})")
                
                if optimal_bet >= 1.0: # Polymarket mini bet size roughly ~$1
                    logger.info("🚀 Розмір ставки валідний. Виконання...")
                    
                    # Відправка в Telegram
                    mode_str = "🎮 ARENA" if SIMULATION_MODE else "🔴 LIVE"
                    msg = (f"🚀 <b>MoneyPigBot торгує! [{mode_str}] ({source})</b>\n\n"
                           f"<b>Ринок:</b> {title}\n"
                           f"<b>Рішення:</b> {direction.upper()}\n"
                           f"<b>Ціна:</b> ${pm_price}\n"
                           f"<b>Впевненість ШІ:</b> {confidence}%\n"
                           f"<b>Ставка (Quarter-Kelly):</b> ${optimal_bet}")
                    await send_telegram_message(msg)
                    
                    await execute_trade(opp, ai_decision, optimal_bet)
                    PROCESSED_SLUGS.add(slug)
                else:
                    logger.info("⛔ Ставка занадто мала, пропускаємо (Kelly < $1.0) або немає математичної переваги.")
            else:
                logger.info("⛔ Відхилено ШІ.")
                
    except Exception as e:
        logger.error(f"Помилка під час обробки події: {e}")

async def run_autonomous_scanner():
    """Фонова задача, яка перевіряє арбітражні можливості самостійно кожні 15 хвилин."""
    # Інтервал 900 сек (15 хв) замість 300 сек, щоб не перевищити ліміт Gemini Free Tier (1500 RPD)
    logger.info(f"⏳ Автономний сканер запущено. Інтервал: 900 сек.")
    while True:
        try:
            logger.info("🔍 [Сканер] Шукаю нові арбітражні ситуації...")
            await process_opportunities_on_event(source="SCANNER")
        except Exception as e:
            logger.error(f"Помилка в автономному сканері: {e}")
        await asyncio.sleep(900)

async def main_loop():
    logger.info("=== Запуск Polymarket AI Bot (MoneyPigBot) ===")
    logger.info(f"Режим: {'SIMULATION (Arena)' if SIMULATION_MODE else 'LIVE TRADING'}")
    
    await init_db()
    await load_processed_slugs()
    await check_arena_portfolio()
    await fetch_initial_whales()
    
    logger.info("📡 Запуск WebSocket слухача та Автономного сканера...")
    
    # Запускаємо автономний сканер як фонову задачу
    asyncio.create_task(run_autonomous_scanner())
    
    # Запускаємо WebSockets listener (він блокує виконання)
    await listen_to_whales_ws()

if __name__ == "__main__":
    try:
        asyncio.run(main_loop())
    except KeyboardInterrupt:
        logger.info("Бота зупинено користувачем.")
