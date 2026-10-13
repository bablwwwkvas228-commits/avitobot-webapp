import asyncio
import logging
import sqlite3
import re
import os
from datetime import datetime
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton

from curl_cffi.requests import AsyncSession
from bs4 import BeautifulSoup

# Загружаем переменные из .env файла (если он есть локально)
load_dotenv()

# ================= 1. НАСТРОЙКИ =================
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHECK_INTERVAL_SECONDS = 40  # Пауза между проверками

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ================= 2. БАЗА ДАННЫХ =================
def init_db():
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS searches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            url TEXT,
            max_price INTEGER
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS seen_items (
            item_id TEXT PRIMARY KEY
        )
    """)
    conn.commit()
    conn.close()

def add_search(user_id: int, url: str, max_price: int):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("INSERT INTO searches (user_id, url, max_price) VALUES (?, ?, ?)", (user_id, url, max_price))
    conn.commit()
    conn.close()

def get_all_searches():
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT id, user_id, url, max_price FROM searches")
    rows = cursor.fetchall()
    conn.close()
    return rows

def delete_search(search_id: int):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM searches WHERE id = ?", (search_id,))
    conn.commit()
    conn.close()

def is_item_seen(item_id: str) -> bool:
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM seen_items WHERE item_id = ?", (item_id,))
    result = cursor.fetchone()
    conn.close()
    return result is not None

def mark_item_as_seen(item_id: str):
    conn = sqlite3.connect("bot_database.db")
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO seen_items (item_id) VALUES (?)", (item_id,))
    conn.commit()
    conn.close()

# ================= 3. ПАРСИНГ =================
def format_avito_url(url: str) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    query['s'] = ['104']
    new_query = urlencode(query, doseq=True)
    return urlunparse(parsed._replace(query=new_query))

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

            # Парсим все объявления на странице (обычно 50 штук) для точного расчета средней цены
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

                geo_elem = card.select_one('[class*="geo-"]')
                location = geo_elem.get_text(strip=True) if geo_elem else "Не указано"

                rating_elem = card.select_one('[data-marker="item-rating"]')
                reviews = rating_elem.get_text(strip=True) if rating_elem else "Нет отзывов"

                date_elem = card.select_one('[data-marker="item-date"]')
                ad_date = date_elem.get_text(strip=True) if date_elem else "Неизвестно"

                results.append({
                    "id": item_id,
                    "title": title,
                    "price": price,
                    "url": url,
                    "location": location,
                    "reviews": reviews,
                    "ad_date": ad_date
                })

    except Exception as e:
        logging.error(f"Ошибка запроса: {e}")

    return results

# ================= 4. АЛГОРИТМ РАСЧЕТА СРЕДНЕЙ ЦЕНЫ МОДЕЛИ =================
def get_model_average(target_title: str, items: list) -> int:
    """Вычисляет среднюю цену только среди похожих моделей с такой же памятью."""
    def normalize_title(t):
        t = t.lower()
        # Отделяем цифры от букв (чтобы "128gb" превратилось в "128 gb")
        t = re.sub(r'(\d+)([a-zа-я]+)', r'\1 \2', t)
        t = re.sub(r'([a-zа-я]+)(\d+)', r'\1 \2', t)
        # Оставляем только буквы и цифры
        t = re.sub(r'[^\w\s]', ' ', t)
        tokens = set(t.split())
        numbers = set(re.findall(r'\d+', t))
        return tokens, numbers

    target_tokens, target_numbers = normalize_title(target_title)
    
    # Ключевые модификаторы, которые строго отличают модели
    modifiers = {'pro', 'max', 'plus', 'ultra', 'mini', 'lite', 'fe'}
    target_mods = target_tokens.intersection(modifiers)

    similar_prices = []
    for item in items:
        if item["price"] <= 0:
            continue
            
        item_tokens, item_numbers = normalize_title(item["title"])
        
        # 1. Проверяем модификаторы (если ищем Pro Max, то обычный Pro не пройдет)
        if target_mods != item_tokens.intersection(modifiers):
            continue

        # 2. Проверяем цифры (если ищем 15 и 128, то в аналоге тоже должны быть 15 и 128)
        if target_numbers and not target_numbers.issubset(item_numbers):
            continue

        # 3. Если в названии вообще нет цифр, проверяем совпадение текста на 50%
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

if not BOT_TOKEN:
    raise ValueError("ОШИБКА: Токен бота не найден! Проверьте файл .env или настройки хостинга.")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

main_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="➕ Добавить поиск")],
        [KeyboardButton(text="📋 Мои поиски")]
    ],
    resize_keyboard=True,
    input_field_placeholder="Выберите действие..."
)

@dp.message(CommandStart())
async def cmd_start(msg: Message):
    await msg.answer(
        "👋 <b>Бот поиска свежих объявлений на Авито</b>\n\n"
        "Выберите действие на клавиатуре ниже:",
        reply_markup=main_keyboard,
        parse_mode="HTML"
    )

@dp.message(F.text == "➕ Добавить поиск")
async def btn_add(msg: Message, state: FSMContext):
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
            f"🔗 <a href='{url}'>Ссылка поиска</a>",
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
                items = await fetch_ads_direct(url)
                
                if not items:
                    continue

                for item in items:
                    item_id = item["id"]

                    if not is_item_seen(item_id):
                        mark_item_as_seen(item_id)

                        if 0 < item["price"] <= max_price:
                            # Вычисляем среднюю цену именно для этой модели
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

                            current_time = datetime.now().strftime("%d.%m.%Y %H:%M")

                            text = (
                                f"📌 <b>{item['title']}</b>\n"
                                f"💰 Цена: <b>{item['price']:,} ₽</b>\n"
                                f"👤 Продавец · {item['reviews']}\n"
                                f"🌍 Местоположение: {item['location']}\n"
                                f"📊 {stats_text}\n\n"
                                f"🔗 {item['url']}\n"
                                f"🕒 {item['ad_date']} (Найдено: {current_time})"
                            )
                            try:
                                await bot.send_message(chat_id=user_id, text=text, parse_mode="HTML", disable_web_page_preview=True)
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