import asyncio
import logging
import sqlite3
import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton

from curl_cffi.requests import AsyncSession
from bs4 import BeautifulSoup

# ================= 1. НАСТРОЙКИ =================
BOT_TOKEN = "8735276903:AAH-0nm_PSEaMHZ3Bqk76rAKDZArYlpLfoc"
CHECK_INTERVAL_SECONDS = 40  # Пауза между проверками
DB_PATH = "data/bot_database.db"  # Папка data сохраняется при пересборках на Bothost

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ================= 2. БАЗА ДАННЫХ =================
def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Таблица пользователей и подписок
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            sub_until TEXT,
            plan TEXT DEFAULT 'free'
        )
    """)
    
    # Таблица поисков
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS searches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            url TEXT,
            max_price INTEGER
        )
    """)
    
    # Таблица просмотренных объявлений (с привязкой к конкретному поиску!)
    cursor.execute("PRAGMA table_info(seen_items)")
    columns = [col[1] for col in cursor.fetchall()]
    if columns and "search_id" not in columns:
        # Пересоздаем старую таблицу, если она была без search_id
        cursor.execute("DROP TABLE seen_items")
        
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS seen_items (
            search_id INTEGER,
            item_id TEXT,
            PRIMARY KEY (search_id, item_id)
        )
    """)
    conn.commit()
    conn.close()

def register_user(user_id: int, username: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    if not cursor.fetchone():
        # Новым пользователям можно давать, например, 1 пробный день (или оставлять без подписки)
        cursor.execute(
            "INSERT INTO users (user_id, username, sub_until, plan) VALUES (?, ?, ?, ?)",
            (user_id, username or "User", None, "free")
        )
    else:
        cursor.execute("UPDATE users SET username = ? WHERE user_id = ?", (username or "User", user_id))
    conn.commit()
    conn.close()

def set_subscription(user_id: int, days: int, plan: str = "pro") -> str:
    tz_moscow = timezone(timedelta(hours=3))
    now = datetime.now(tz_moscow)
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT sub_until FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    
    base_date = now
    if row and row[0]:
        try:
            current_sub = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz_moscow)
            if current_sub > now:
                base_date = current_sub
        except Exception:
            pass
            
    new_sub_date = base_date + timedelta(days=days)
    new_sub_str = new_sub_date.strftime("%Y-%m-%d %H:%M:%S")
    
    cursor.execute("""
        INSERT INTO users (user_id, username, sub_until, plan)
        VALUES (?, 'User', ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET sub_until = ?, plan = ?
    """, (user_id, new_sub_str, plan, new_sub_str, plan))
    conn.commit()
    conn.close()
    return new_sub_date.strftime("%d.%m.%Y %H:%M")

def check_subscription(user_id: int) -> tuple[bool, str]:
    tz_moscow = timezone(timedelta(hours=3))
    now = datetime.now(tz_moscow)
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT sub_until FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    
    if not row or not row[0]:
        return False, "Не активна"
        
    try:
        sub_date = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz_moscow)
        if sub_date > now:
            return True, sub_date.strftime("%d.%m.%Y %H:%M")
        else:
            return False, "Истекла"
    except Exception:
        return False, "Не активна"

def add_search(user_id: int, url: str, max_price: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO searches (user_id, url, max_price) VALUES (?, ?, ?)", (user_id, url, max_price))
    conn.commit()
    conn.close()

def get_all_searches():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id, user_id, url, max_price FROM searches")
    rows = cursor.fetchall()
    conn.close()
    return rows

def delete_search(search_id: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM searches WHERE id = ?", (search_id,))
    cursor.execute("DELETE FROM seen_items WHERE search_id = ?", (search_id,))
    conn.commit()
    conn.close()

def is_item_seen(search_id: int, item_id: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM seen_items WHERE search_id = ? AND item_id = ?", (search_id, item_id))
    result = cursor.fetchone()
    conn.close()
    return result is not None

def mark_item_as_seen(search_id: int, item_id: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO seen_items (search_id, item_id) VALUES (?, ?)", (search_id, item_id))
    conn.commit()
    conn.close()

# ================= 3. ПАРСИНГ =================
def format_avito_url(url: str) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    query['s'] = ['104']
    new_query = urlencode(query, doseq=True)
    return urlunparse(parsed._replace(query=new_query))

def parse_avito_date(date_str: str) -> str:
    if not date_str:
        return "Неизвестно"
        
    tz_moscow = timezone(timedelta(hours=3))
    now = datetime.now(tz_moscow)
    date_str = date_str.lower().strip()

    try:
        if "только что" in date_str:
            return now.strftime("%d.%m.%Y %H:%M")

        match_min = re.search(r'(\d+)\s*минут', date_str)
        if match_min:
            dt = now - timedelta(minutes=int(match_min.group(1)))
            return dt.strftime("%d.%m.%Y %H:%M")
        if "минуту назад" in date_str:
            dt = now - timedelta(minutes=1)
            return dt.strftime("%d.%m.%Y %H:%M")

        match_hour = re.search(r'(\d+)\s*час', date_str)
        if match_hour:
            dt = now - timedelta(hours=int(match_hour.group(1)))
            return dt.strftime("%d.%m.%Y %H:%M")
        if "час назад" in date_str:
            dt = now - timedelta(hours=1)
            return dt.strftime("%d.%m.%Y %H:%M")

        match_today = re.search(r'сегодня\s*(?:в)?\s*(\d{1,2}):(\d{2})', date_str)
        if match_today:
            dt = now.replace(hour=int(match_today.group(1)), minute=int(match_today.group(2)), second=0, microsecond=0)
            return dt.strftime("%d.%m.%Y %H:%M")

        match_yesterday = re.search(r'вчера\s*(?:в)?\s*(\d{1,2}):(\d{2})', date_str)
        if match_yesterday:
            dt = (now - timedelta(days=1)).replace(hour=int(match_yesterday.group(1)), minute=int(match_yesterday.group(2)), second=0, microsecond=0)
            return dt.strftime("%d.%m.%Y %H:%M")

        return date_str.capitalize()
    except Exception as e:
        logging.warning(f"Не удалось распарсить дату: {date_str} - {e}")
        return date_str.capitalize()

async def fetch_ads_direct(search_url: str) -> list[dict]:
    target_url = format_avito_url(search_url)
    results = []

    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Upgrade-Insecure-Requests": "1",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    }

    try:
        async with AsyncSession(impersonate="chrome120") as session:
            resp = await session.get(target_url, headers=headers, timeout=20)
            
            if resp.status_code != 200:
                logging.warning(f"Статус страницы: {resp.status_code}")
                return []

            soup = BeautifulSoup(resp.text, "html.parser")
            cards = soup.select('div[data-marker="item"]')

            for card in cards:
                item_id = card.get("data-item-id")
                if not item_id:
                    continue

                title_elem = card.select_one('[data-marker="item-title"]')
                title = title_elem.get_text(strip=True) if title_elem else "Без названия"

                raw_href = title_elem.get("href") if title_elem else ""
                url = f"https://www.avito.ru{raw_href}" if raw_href.startswith("/") else raw_href

                price_meta = card.select_one('meta[itemprop="price"]')
                price = int(price_meta.get("content", 0)) if price_meta and price_meta.get("content") else 0

                location = "Локация не указана"
                geo_elem = (
                    card.find(attrs={"data-marker": "item-address"}) or 
                    card.select_one('[class*="geo-"]') or 
                    card.select_one('[class*="address-"]') or
                    card.select_one('[class*="location-"]')
                )
                if geo_elem:
                    location = geo_elem.get_text(separator=" ", strip=True)
                else:
                    for span in card.find_all(['span', 'div']):
                        txt = span.get_text(strip=True)
                        if ("," in txt or "район" in txt.lower() or "область" in txt.lower()) and 3 < len(txt) < 40 and "₽" not in txt:
                            location = txt
                            break

                location = re.sub(r'\s+', ' ', location).strip()

                date_elem = card.select_one('[data-marker="item-date"]')
                raw_date = date_elem.get_text(strip=True) if date_elem else ""
                ad_date = parse_avito_date(raw_date)

                results.append({
                    "id": item_id,
                    "title": title,
                    "price": price,
                    "url": url,
                    "location": location,
                    "ad_date": ad_date
                })

    except Exception as e:
        logging.error(f"Ошибка запроса: {e}")

    return results

# ================= 4. АЛГОРИТМ РАСЧЕТА СРЕДНЕЙ ЦЕНЫ МОДЕЛИ =================
def get_model_average(target_title: str, items: list) -> int:
    def normalize_title(t):
        t = t.lower()
        t = re.sub(r'(\d+)([a-zа-я]+)', r'\1 \2', t)
        t = re.sub(r'([a-zа-я]+)(\d+)', r'\1 \2', t)
        t = re.sub(r'[^\w\s]', ' ', t)
        tokens = set(t.split())
        numbers = set(re.findall(r'\d+', t))
        return tokens, numbers

    target_tokens, target_numbers = normalize_title(target_title)
    modifiers = {'pro', 'max', 'plus', 'ultra', 'mini', 'lite', 'fe'}
    target_mods = target_tokens.intersection(modifiers)

    similar_prices = []
    for item in items:
        if item["price"] <= 0:
            continue
            
        item_tokens, item_numbers = normalize_title(item["title"])
        
        if target_mods != item_tokens.intersection(modifiers):
            continue

        if target_numbers and not target_numbers.issubset(item_numbers):
            continue

        if not target_numbers and len(target_tokens) > 0:
            ratio = len(target_tokens.intersection(item_tokens)) / len(target_tokens)
            if ratio < 0.5:
                continue

        similar_prices.append(item["price"])

    if not similar_prices:
        return 0
    
    return sum(similar_prices) // len(similar_prices)

# ================= 5. TELEGRAM БОТ (Интерфейс) =================
class SearchForm(StatesGroup):
    waiting_for_url = State()
    waiting_for_price = State()

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

main_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="➕ Добавить поиск"), KeyboardButton(text="📋 Мои поиски")],
        [KeyboardButton(text="💎 Подписка / Профиль")]
    ],
    resize_keyboard=True,
    input_field_placeholder="Выберите действие..."
)

@dp.message(CommandStart())
async def cmd_start(msg: Message):
    register_user(msg.from_user.id, msg.from_user.username)
    await msg.answer(
        "👋 <b>Бот поиска свежих объявлений на Авито</b>\n\n"
        "Выберите действие на клавиатуре ниже:",
        reply_markup=main_keyboard,
        parse_mode="HTML"
    )

@dp.message(Command("give_sub"))
async def cmd_give_sub(msg: Message):
    """Команда для выдачи подписки: /give_sub USER_ID DAYS"""
    parts = msg.text.split()
    if len(parts) < 3:
        await msg.answer("ℹ️ Использование: <code>/give_sub ID_пользователя количество_дней</code>\nНапример: <code>/give_sub 123456789 30</code>", parse_mode="HTML")
        return
    try:
        target_id = int(parts[1])
        days = int(parts[2])
        until_str = set_subscription(target_id, days)
        await msg.answer(f"✅ Пользователю <code>{target_id}</code> выдана подписка на {days} дн. (до {until_str} МСК)", parse_mode="HTML")
        try:
            await bot.send_message(target_id, f"🎉 Вам активирована подписка до <b>{until_str} (МСК)</b>!", parse_mode="HTML")
        except Exception:
            pass
    except ValueError:
        await msg.answer("❌ Ошибка: ID пользователя и количество дней должны быть числами.")

@dp.message(F.text == "💎 Подписка / Профиль")
async def btn_profile(msg: Message):
    register_user(msg.from_user.id, msg.from_user.username)
    is_active, sub_text = check_subscription(msg.from_user.id)
    status_emoji = "🟢 Активна до " + sub_text if is_active else "🔴 " + sub_text
    
    searches = get_all_searches()
    user_searches_count = len([s for s in searches if s[1] == msg.from_user.id])

    await msg.answer(
        f"👤 <b>Ваш профиль</b>\n\n"
        f"🆔 Ваш ID: <code>{msg.from_user.id}</code>\n"
        f"💎 Статус подписки: <b>{status_emoji}</b>\n"
        f"🔍 Активных поисков: <b>{user_searches_count}</b>\n\n"
        f"<i>Скоро здесь появится кнопка открытия Mini App для удобного управления тарифами!</i>",
        parse_mode="HTML"
    )

@dp.message(F.text == "➕ Добавить поиск")
async def btn_add(msg: Message, state: FSMContext):
    is_active, _ = check_subscription(msg.from_user.id)
    if not is_active:
        await msg.answer(
            "⛔️ <b>У вас нет активной подписки!</b>\n\n"
            "Перейдите в раздел «💎 Подписка / Профиль», чтобы узнать ваш ID и активировать доступ.",
            parse_mode="HTML"
        )
        return

    await msg.answer("🔗 Отправьте <b>ссылку на поиск с Авито</b>:", parse_mode="HTML")
    await state.set_state(SearchForm.waiting_for_url)

@dp.message(SearchForm.waiting_for_url)
async def process_url(msg: Message, state: FSMContext):
    url = msg.text.strip()
    if not url.startswith("http"):
        await msg.answer("Пожалуйста, отправьте корректную ссылку, начинающуюся с http:// или https://")
        return

    await state.update_data(url=url)
    await msg.answer("💰 Введите <b>максимальную цену</b> в рублях:", parse_mode="HTML")
    await state.set_state(SearchForm.waiting_for_price)

@dp.message(SearchForm.waiting_for_price)
async def process_price(msg: Message, state: FSMContext):
    digits = re.sub(r"\D", "", msg.text)
    if not digits:
        await msg.answer("Введите число (только цифры):")
        return

    max_price = int(digits)
    data = await state.get_data()
    url = data["url"]

    add_search(msg.from_user.id, url, max_price)
    await state.clear()
    await msg.answer(
        f"✅ <b>Поиск успешно добавлен!</b>\n"
        f"💰 Макс. цена: {max_price:,} ₽\n\n"
        f"Бот отслеживает новые объявления.",
        reply_markup=main_keyboard,
        parse_mode="HTML"
    )

@dp.message(F.text == "📋 Мои поиски")
async def btn_list(msg: Message):
    searches = get_all_searches()
    user_searches = [s for s in searches if s[1] == msg.from_user.id]

    if not user_searches:
        await msg.answer("У вас пока нет активных поисков. Нажмите «➕ Добавить поиск».")
        return

    for s_id, _, url, max_price in user_searches:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"del_{s_id}")
        ]])
        await msg.answer(
            f"🔍 <b>Поиск #{s_id}</b>\n"
            f"💰 Лимит: до {max_price:,} ₽\n"
            f"🔗 <a href='{url}'>Открыть поиск на Авито</a>",
            reply_markup=kb,
            parse_mode="HTML"
        )

@dp.callback_query(F.data.startswith("del_"))
async def delete_callback(callback: CallbackQuery):
    s_id = int(callback.data.split("_")[1])
    delete_search(s_id)
    await callback.message.edit_text("❌ Поиск удален.")
    await callback.answer()

# ================= 6. ФОНОВЫЙ МОНИТОРИНГ =================
async def monitor_task():
    while True:
        try:
            searches = get_all_searches()
            for s_id, user_id, url, max_price in searches:
                # Проверяем, активна ли подписка у владельца поиска
                is_active, _ = check_subscription(user_id)
                if not is_active:
                    continue

                items = await fetch_ads_direct(url)
                
                if not items:
                    continue

                for item in items:
                    item_id = item["id"]

                    if not is_item_seen(s_id, item_id):
                        mark_item_as_seen(s_id, item_id)

                        if 0 < item["price"] <= max_price:
                            avg_price = get_model_average(item["title"], items)
                            
                            stats_text = ""
                            if avg_price > 0:
                                diff = item["price"] - avg_price
                                percent = abs(int((diff / avg_price) * 100))
                                if diff < 0:
                                    stats_text = f"📉 На {percent}% ниже рынка ({avg_price:,} ₽)"
                                elif diff > 0:
                                    stats_text = f"📈 На {percent}% выше рынка ({avg_price:,} ₽)"
                                else:
                                    stats_text = f"⚖️ Равна средней цене ({avg_price:,} ₽)"
                            else:
                                stats_text = "🤷‍♂️ Нет данных для сравнения точной модели"

                            safe_title = item['title'].replace('<', '&lt;').replace('>', '&gt;')
                            
                            text = (
                                f"📌 <b>{safe_title}</b>\n"
                                f"💰 Цена: <b>{item['price']:,} ₽</b>\n"
                                f"🌍 {item['location']}\n"
                                f"📊 {stats_text}\n\n"
                                f"🕒 Опубликовано: {item['ad_date']}\n\n"
                                f"🔗 <a href='{item['url']}'>Открыть на Авито</a>"
                            )
                            try:
                                await bot.send_message(chat_id=user_id, text=text, parse_mode="HTML")
                            except Exception as err:
                                logging.error(f"Ошибка отправки сообщения: {err}")

                await asyncio.sleep(4)

        except Exception as e:
            logging.error(f"Ошибка в цикле мониторинга: {e}")

        await asyncio.sleep(CHECK_INTERVAL_SECONDS)

# ================= 7. ТОЧКА ВХОДА =================
async def main():
    init_db()
    asyncio.create_task(monitor_task())
    logging.info("Бот запущен...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())