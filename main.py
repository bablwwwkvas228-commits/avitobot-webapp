import asyncio
import logging
import sqlite3
import re
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from playwright.async_api import async_playwright

# ================= 1. НАСТРОЙКИ =================
BOT_TOKEN = "8735276903:AAH-0nm_PSEaMHZ3Bqk76rAKDZArYlpLfoc"
CHECK_INTERVAL_SECONDS = 40  # Оптимальная задержка между проверками

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

# ================= 3. ПАРСИНГ ЧЕРЕЗ БРАУЗЕР (ОБХОД 403) =================
def format_avito_url(url: str) -> str:
    """Гарантирует сортировку по дате (s=104)."""
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    query['s'] = ['104']
    new_query = urlencode(query, doseq=True)
    return urlunparse(parsed._replace(query=new_query))

async def fetch_ads_via_browser(search_url: str) -> list[dict]:
    target_url = format_avito_url(search_url)
    results = []

    try:
        async with async_playwright() as p:
            # Запускаем браузер в максимально легком режиме для слабого хостинга
            browser = await p.chromium.launch(
                headless=True,
                channel="chrome",
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-crash-reporter",
                    "--no-zygote",
                    "--single-process"
                ]
            )
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 800}
            )
            page = await context.new_page()

            # Переходим по ссылке
            response = await page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
            
            if response and response.status == 403:
                logging.warning("Сайт вернул 403 даже в браузере. Попробуйте раздать интернет с телефона.")
                await browser.close()
                return []

            # Ждём пару секунд для отрисовки карточек
            await asyncio.sleep(3)

            cards = await page.query_selector_all('div[data-marker="item"]')

            for card in cards[:10]:
                item_id = await card.get_attribute("data-item-id")
                if not item_id:
                    continue

                title_elem = await card.query_selector('h3[itemprop="name"]')
                title = await title_elem.inner_text() if title_elem else "Без названия"

                link_elem = await card.query_selector('a[data-marker="item-title"]')
                raw_href = await link_elem.get_attribute("href") if link_elem else ""
                url = f"https://www.avito.ru{raw_href}" if raw_href.startswith("/") else raw_href

                price_meta = await card.query_selector('meta[itemprop="price"]')
                price = 0
                if price_meta:
                    val = await price_meta.get_attribute("content")
                    if val:
                        price = int(val)

                # Ищем ссылку на фотографию
                try:
                    img_element = await card.query_selector('img[itemprop="image"], img[class*="photo"], img')
                    photo_url = await img_element.get_attribute("src") if img_element else None
                except Exception:
                    photo_url = None

                results.append({
                    "id": item_id,
                    "title": title.strip(),
                    "price": price,
                    "url": url,
                    "photo_url": photo_url
                })

            await browser.close()
    except Exception as e:
        logging.error(f"Ошибка при работе браузера: {e}")

    return results

# ================= 4. TELEGRAM БОТ =================
class SearchForm(StatesGroup):
    waiting_for_url = State()
    waiting_for_price = State()

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

@dp.message(CommandStart())
async def cmd_start(msg: Message):
    await msg.answer(
        "👋 <b>Бот поиска свежих объявлений на Авито</b>\n\n"
        "Команды:\n"
        "➕ /add — добавить ссылку на поиск с фильтрами\n"
        "📋 /list — список активных поисков",
        parse_mode="HTML"
    )

@dp.message(Command("add"))
async def cmd_add(msg: Message, state: FSMContext):
    await msg.answer("🔗 Отправьте <b>ссылку на поиск с Авито</b> (настройте фильтры и скопируйте ссылку из браузера):", parse_mode="HTML")
    await state.set_state(SearchForm.waiting_for_url)

@dp.message(SearchForm.waiting_for_url)
async def process_url(msg: Message, state: FSMContext):
    url = msg.text.strip()
    if not url.startswith("http"):
        await msg.answer("Пожалуйста, отправьте корректную ссылку, начинающуюся с http:// или https://")
        return

    await state.update_data(url=url)
    await msg.answer("💰 Теперь введите <b>максимальную цену</b> в рублях (например: <code>35000</code>):", parse_mode="HTML")
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
        f"Бот начал отслеживать свежие лоты по вашей ссылке.",
        parse_mode="HTML"
    )

@dp.message(Command("list"))
async def cmd_list(msg: Message):
    searches = get_all_searches()
    user_searches = [s for s in searches if s[1] == msg.from_user.id]

    if not user_searches:
        await msg.answer("У вас пока нет активных поисков. Добавьте через /add.")
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

# ================= 5. ФОНОВЫЙ МОНИТОРИНГ =================
async def monitor_task():
    while True:
        try:
            searches = get_all_searches()
            for s_id, user_id, url, max_price in searches:
                items = await fetch_ads_via_browser(url)
                for item in items:
                    item_id = item["id"]

                    if not is_item_seen(item_id):
                        mark_item_as_seen(item_id)

                        if item["price"] <= max_price and item["price"] > 0:
                            # Формируем кнопку перехода
                            keyboard = InlineKeyboardMarkup(
                                inline_keyboard=[
                                    [InlineKeyboardButton(text="🔗 Открыть на Авито", url=item['url'])]
                                ]
                            )
                            
                            # Формируем текст
                            caption_text = (
                                f"🔥 <b>Новое объявление!</b>\n\n"
                                f"📱 <b>{item['title']}</b>\n"
                                f"💰 Цена: <b>{item['price']:,} ₽</b>\n"
                            )

                            try:
                                # Отправляем с фото, если оно есть
                                if item.get('photo_url') and item['photo_url'].startswith("http"):
                                    await bot.send_photo(
                                        chat_id=user_id,
                                        photo=item['photo_url'],
                                        caption=caption_text,
                                        parse_mode="HTML",
                                        reply_markup=keyboard
                                    )
                                # Иначе отправляем просто текст
                                else:
                                    await bot.send_message(
                                        chat_id=user_id,
                                        text=caption_text,
                                        parse_mode="HTML",
                                        reply_markup=keyboard
                                    )
                            except Exception as err:
                                logging.error(f"Ошибка отправки сообщения: {err}")

                await asyncio.sleep(5)  # Небольшая пауза между проверками разных ссылок
        except Exception as e:
            logging.error(f"Ошибка в цикле мониторинга: {e}")

        await asyncio.sleep(CHECK_INTERVAL_SECONDS)

# ================= 6. ТОЧКА ВХОДА =================
async def main():
    init_db()
    asyncio.create_task(monitor_task())
    logging.info("Бот запущен...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())