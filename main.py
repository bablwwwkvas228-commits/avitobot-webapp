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
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, 
    ReplyKeyboardMarkup, KeyboardButton,
    PreCheckoutQuery, LabeledPrice
)

from curl_cffi.requests import AsyncSession
from bs4 import BeautifulSoup

# ================= 1. НАСТРОЙКИ =================
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("ОШИБКА: Переменная окружения BOT_TOKEN не задана в настройках хостинга!")

CHECK_INTERVAL_SECONDS = 40
DB_PATH = "data/bot_database.db"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ================= 2. БАЗА ДАННЫХ =================
def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            sub_until TEXT,
            plan TEXT DEFAULT 'free'
        )
    """)
    
    # Проверяем наличие колонки plan, если таблица старая
    cursor.execute("PRAGMA table_info(users)")
    user_columns = [col[1] for col in cursor.fetchall()]
    if "plan" not in user_columns:
        cursor.execute("ALTER TABLE users ADD COLUMN plan TEXT DEFAULT 'free'")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS searches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            url TEXT,
            max_price INTEGER
        )
    """)
    
    cursor.execute("PRAGMA table_info(seen_items)")
    columns = [col[1] for col in cursor.fetchall()]
    if columns and "search_id" not in columns:
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
        tz_moscow = timezone(timedelta(hours=3))
        trial_until = (datetime.now(tz_moscow) + timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute(
            "INSERT INTO users (user_id, username, sub_until, plan) VALUES (?, ?, ?, ?)",
            (user_id, username or "User", trial_until, "start")
        )
    else:
        cursor.execute("UPDATE users SET username = ? WHERE user_id = ?", (username or "User", user_id))
    conn.commit()
    conn.close()

def set_subscription(user_id: int, days: int, plan: str = "start") -> str:
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

def check_subscription(user_id: int) -> tuple[bool, str, str]:
    tz_moscow = timezone(timedelta(hours=3))
    now = datetime.now(tz_moscow)
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT sub_until, plan FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    
    if not row or not row[0]:
        return False, "Не активна", "start"
        
    try:
        sub_date = datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz_moscow)
        plan = row[1] or "start"
        if sub_date > now:
            return True, sub_date.strftime("%d.%m.%Y %H:%M"), plan
        else:
            return False, "Истекла", plan
    except Exception:
        return False, "Не активна", "start"

def get_user_search_limit(user_id: int) -> tuple[int, str]:
    """Возвращает лимит поисков и текущий уровень тарифа пользователя"""
    is_active, _, plan = check_subscription(user_id)
    if not is_active:
        return 0, "free"
    
    # Лимиты: для Start — до 3 поисков, для Pro — до 15 поисков
    if plan == "pro":
        return 15, "pro"
    return 3, "start"

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
    except Exception:
        return date_str.capitalize()

async def fetch_ads_direct(search_url: str) -> list[dict]:
    target_url = format_avito_url(search_url)
    results = []
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    }
    for attempt in range(2):
        try:
            async with AsyncSession(impersonate="chrome120") as session:
                resp = await session.get(target_url, headers=headers, timeout=35)
                if resp.status_code != 200:
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
                    geo_elem = card.find(attrs={"data-marker": "item-address"}) or card.select_one('[class*="geo-"]')
                    if geo_elem:
                        location = geo_elem.get_text(separator=" ", strip=True)

                    date_elem = card.select_one('[data-marker="item-date"]')
                    raw_date = date_elem.get_text(strip=True) if date_elem else ""
                    ad_date = parse_avito_date(raw_date)

                    results.append({
                        "id": item_id, "title": title, "price": price,
                        "url": url, "location": location, "ad_date": ad_date
                    })
                return results
        except Exception as e:
            logging.error(f"Попытка {attempt + 1} - Ошибка запроса к Авито: {e}")
            await asyncio.sleep(5)
    return results

# ================= 4. TELEGRAM БОТ =================
class SearchForm(StatesGroup):
    waiting_for_url = State()
    waiting_for_price = State()

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

main_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="➕ Добавить поиск"), KeyboardButton(text="📋 Мои поиски")],
        [KeyboardButton(text="💎 Подписка / Тарифы")]
    ],
    resize_keyboard=True,
    input_field_placeholder="Выберите действие..."
)

@dp.message(CommandStart())
async def cmd_start(msg: Message):
    register_user(msg.from_user.id, msg.from_user.username)
    
    args = msg.text.split()
    if len(args) > 1:
        # Бесплатный пробный период (2 дня, тариф Start)
        if args[1].startswith("free_"):
            try:
                days = int(args[1].split("_")[1])
                until_str = set_subscription(msg.from_user.id, days, plan="start")
                await msg.answer(
                    f"🎉 <b>Пробный период Start активирован!</b>\n"
                    f"Вам добавлено <b>{days} дня(дней)</b> бесплатного доступа (лимит: до 3 поисков).\n"
                    f"Действует до: <b>{until_str} МСК</b> 🚀",
                    parse_mode="HTML"
                )
                return
            except Exception as e:
                logging.error(f"Ошибка активации пробного периода: {e}")
                
        # Покупка платных тарифов через Stars (start или pro)
        elif args[1].startswith("buy_"):
            try:
                # Формат: buy_start_30_100 или buy_pro_30_200
                _, plan_type, days_str, stars_str = args[1].split("_")
                days, stars = int(days_str), int(stars_str)
                
                plan_title = "Start" if plan_type == "start" else "Pro (Всё включено)"
                prices = [LabeledPrice(label=f"Тариф {plan_title} ({days} дн.)", amount=stars)]
                
                await bot.send_invoice(
                    chat_id=msg.from_user.id,
                    title=f"FlipRadar • Тариф {plan_title} ({days} дн.)",
                    description=f"Активация подписки FlipRadar на {days} дней.",
                    payload=f"sub_{plan_type}_{days}",
                    currency="XTR",
                    prices=prices
                )
                return
            except Exception as e:
                logging.error(f"Ошибка выставления счета: {e}")

    is_active, sub_text, plan = check_subscription(msg.from_user.id)
    status_emoji = f"🟢 Активна до {sub_text} ({plan.upper()})" if is_active else f"🔴 {sub_text}"
    
    await msg.answer(
        f"👋 <b>FlipRadar • Бот мониторинга и аналитики</b>\n\n"
        f"🆔 Ваш ID: <code>{msg.from_user.id}</code>\n"
        f"💎 Подписка: <b>{status_emoji}</b>\n\n"
        f"Используйте кнопки ниже для управления:",
        reply_markup=main_keyboard,
        parse_mode="HTML"
    )

@dp.message(Command("give_sub"))
async def cmd_give_sub(msg: Message):
    parts = msg.text.split()
    if len(parts) < 3:
        await msg.answer("ℹ️ Использование: <code>/give_sub ID_пользователя дни [start/pro]</code>", parse_mode="HTML")
        return
    try:
        target_id = int(parts[1])
        days = int(parts[2])
        plan = parts[3] if len(parts) > 3 else "pro"
        until_str = set_subscription(target_id, days, plan=plan)
        await msg.answer(f"✅ Пользователю <code>{target_id}</code> выдана подписка <b>{plan.upper()}</b> на {days} дн. до {until_str} МСК", parse_mode="HTML")
    except ValueError:
        await msg.answer("❌ Ошибка в параметрах.")

@dp.pre_checkout_query()
async def pre_checkout_query_handler(pre_checkout_query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@dp.message(F.successful_payment)
async def successful_payment_handler(msg: Message):
    payload = msg.successful_payment.invoice_payload
    if payload.startswith("sub_"):
        # payload формат: sub_start_30 или sub_pro_30
        parts = payload.split("_")
        plan_type = parts[1]
        days = int(parts[2])
        
        until_str = set_subscription(msg.from_user.id, days, plan=plan_type)
        plan_title = "Start" if plan_type == "start" else "Pro"
        
        await msg.answer(
            f"🎉 <b>Оплата прошла успешно!</b>\n"
            f"Активирован тариф <b>{plan_title}</b> на <b>{days} дней</b>.\n"
            f"Действует до: <b>{until_str} МСК</b> 🚀",
            parse_mode="HTML"
        )

@dp.message(F.text == "➕ Добавить поиск")
async def btn_add(msg: Message, state: FSMContext):
    is_active, _, plan = check_subscription(msg.from_user.id)
    if not is_active:
        await msg.answer("⛔️ У вас истекла подписка. Откройте меню Mini App для выбора тарифа и продления!")
        return

    user_searches = [s for s in get_all_searches() if s[1] == msg.from_user.id]
    max_limit, plan_name = get_user_search_limit(msg.from_user.id)
    
    if len(user_searches) >= max_limit:
        upgrade_text = (
            " Перейдите на тариф <b>Pro</b> в меню подписок, чтобы увеличить лимит до 15 поисков и разблокировать аналитику!" 
            if plan_name == "start" 
            else ""
        )
        await msg.answer(
            f"⚠️ <b>Достигнут лимит поисков!</b>\n"
            f"Ваш тариф <b>{plan_name.upper()}</b> позволяет иметь не более <b>{max_limit}</b> активных поисков.{upgrade_text}",
            parse_mode="HTML"
        )
        return

    await msg.answer("🔗 Отправьте <b>ссылку на поиск с Авито</b>:", parse_mode="HTML")
    await state.set_state(SearchForm.waiting_for_url)

@dp.message(SearchForm.waiting_for_url)
async def process_url(msg: Message, state: FSMContext):
    url = msg.text.strip()
    if not url.startswith("http"):
        await msg.answer("Отправьте корректную ссылку, начинающуюся с http:// или https://")
        return
    await state.update_data(url=url)
    await msg.answer("💰 Введите <b>максимальную цену</b> в рублях:", parse_mode="HTML")
    await state.set_state(SearchForm.waiting_for_price)

@dp.message(SearchForm.waiting_for_price)
async def process_price(msg: Message, state: FSMContext):
    digits = re.sub(r"\D", "", msg.text)
    if not digits:
        await msg.answer("Введите число:")
        return
    max_price = int(digits)
    data = await state.get_data()
    add_search(msg.from_user.id, data["url"], max_price)
    await state.clear()
    await msg.answer(f"✅ Поиск успешно добавлен! Макс. цена: {max_price:,} ₽", reply_markup=main_keyboard)

@dp.message(F.text == "📋 Мои поиски")
async def btn_list(msg: Message):
    searches = [s for s in get_all_searches() if s[1] == msg.from_user.id]
    max_limit = get_user_search_limit(msg.from_user.id)
    if not searches:
        await msg.answer(f"У вас нет активных поисков (лимит: 0/{max_limit}).")
        return
    
    await msg.answer(f"📋 Ваши активные поиски ({len(searches)} из {max_limit}):")
    for s_id, _, url, max_price in searches:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🗑 Удалить", callback_data=f"del_{s_id}")]])
        await msg.answer(f"🔍 Поиск #{s_id}\n💰 Лимит: до {max_price:,} ₽\n🔗 <a href='{url}'>Ссылка на Авито</a>", reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data.startswith("del_"))
async def delete_callback(callback: CallbackQuery):
    delete_search(int(callback.data.split("_")[1]))
    await callback.message.edit_text("❌ Поиск удален.")
    await callback.answer()

@dp.message(F.text == "💎 Подписка / Тарифы")
async def btn_sub_info(msg: Message):
    is_active, sub_text, plan = check_subscription(msg.from_user.id)
    status_emoji = f"🟢 Активна до {sub_text}" if is_active else f"🔴 {sub_text}"
    max_limit = get_user_search_limit(msg.from_user.id)
    user_searches_count = len([s for s in get_all_searches() if s[1] == msg.from_user.id])
    
    plan_desc = (
        "<b>📦 Уровень Start:</b> Мониторинг, фильтры (до 3 поисков)" 
        if plan == "start" 
        else "<b>🚀 Уровень Pro:</b> Мониторинг + Аналитика + Учёт сделок (до 15 поисков)"
    )
    
    await msg.answer(
        f"🆔 Ваш ID: <code>{msg.from_user.id}</code>\n"
        f"💎 <b>Статус подписки:</b> {status_emoji}\n"
        f"⭐️ Тариф: <b>{plan.upper()}</b>\n"
        f"{plan_desc}\n"
        f"📊 Активных поисков: <b>{user_searches_count} из {max_limit}</b>\n\n"
        f"💡 Для смены тарифа или продления нажмите на синюю кнопку <b>меню (слева от поля ввода)</b> 👇",
        parse_mode="HTML"
    )

# ================= 5. ФОНОВЫЙ МОНИТОРИНГ =================
async def monitor_task():
    while True:
        try:
            for s_id, user_id, url, max_price in get_all_searches():
                is_active, _, _ = check_subscription(user_id)
                if not is_active:
                    continue

                items = await fetch_ads_direct(url)
                for item in items:
                    if not is_item_seen(s_id, item["id"]):
                        mark_item_as_seen(s_id, item["id"])
                        if 0 < item["price"] <= max_price:
                            safe_title = item['title'].replace('<', '&lt;').replace('>', '&gt;')
                            text = (
                                f"📌 <b>{safe_title}</b>\n"
                                f"💰 Цена: <b>{item['price']:,} ₽</b>\n"
                                f"🌍 {item['location']}\n"
                                f"🕒 Опубликовано: {item['ad_date']}\n\n"
                                f"🔗 <a href='{item['url']}'>Открыть на Авито</a>"
                            )
                            try:
                                await bot.send_message(chat_id=user_id, text=text, parse_mode="HTML")
                            except Exception:
                                pass
                await asyncio.sleep(4)
        except Exception as e:
            logging.error(f"Ошибка мониторинга: {e}")
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)

# ================= 6. ЗАПУСК =================
async def main():
    init_db()
    asyncio.create_task(monitor_task())
    logging.info("FlipRadar запущен с поддержкой Start и Pro тарифов...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())