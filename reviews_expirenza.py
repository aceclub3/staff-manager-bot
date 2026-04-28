import logging
import os
import re
import json
import asyncio
import gspread
import anthropic
import pathlib
from dotenv import load_dotenv
from datetime import datetime
from google.oauth2.service_account import Credentials
from telegram import Update, Bot
from telegram.ext import Application, MessageHandler, filters, ContextTypes

load_dotenv(dotenv_path=pathlib.Path(__file__).parent / ".env")
load_dotenv(dotenv_path=pathlib.Path(__file__).parent / "env")

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Токены ────────────────────────────────────────────────────────────────────
REVIEWS_BOT_TOKEN   = os.getenv("REVIEWS_BOT_TOKEN")       # токен этого бота
FEEDBACK_BOT_TOKEN  = os.getenv("FEEDBACK_BOT_TOKEN")      # токен feedback бота
ANTHROPIC_API_KEY   = os.getenv("ANTHROPIC_API_KEY")
SHEETS_CREDENTIALS  = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
SPREADSHEET_ID      = os.getenv("FEEDBACK_SPREADSHEET_ID")
OWNER_IDS           = [int(x) for x in os.getenv("OWNER_IDS", "").split(",") if x]

# ID чата с отзывами Expirenza (куда добавляем бота)
REVIEWS_CHAT_ID     = int(os.getenv("REVIEWS_CHAT_ID", "0"))

# Имя отправителя сообщений Expirenza в чате
EXPIRENZA_SENDER    = "Expirenza - Відгуки"

RESTAURANT_NAME     = "Терраса"

claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ── Google Sheets ─────────────────────────────────────────────────────────────
def get_sheets_client():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(SHEETS_CREDENTIALS, scopes=scopes)
    return gspread.authorize(creds)

def get_or_create_sheet(spreadsheet, name, rows=2000, cols=10):
    try:
        return spreadsheet.worksheet(name)
    except gspread.WorksheetNotFound:
        sheet = spreadsheet.add_worksheet(name, rows=rows, cols=cols)
        return sheet

def ensure_headers(sheet, headers):
    existing = sheet.row_values(1)
    if not existing or existing[0] != headers[0]:
        sheet.insert_row(headers, index=1)

# ── Парсинг сообщения Expirenza ───────────────────────────────────────────────
def parse_expirenza_message(text: str) -> dict | None:
    """
    Парсит сообщение типа 'Моно-відгук' от Expirenza.
    Возвращает словарь с данными или None если сообщение не подходит.
    """
    if "Моно-відгук" not in text:
        return None
    if "Звіт за" in text or "Загальна статистика" in text:
        return None

    data = {
        "review_text": "",
        "overall": "",        # Відмінно / Добре / Погано / Жахливо
        "waiter": "",
        "table": "",
        "dishes": [],         # [{"name": "...", "rating": "positive"/"negative"/"none"}]
        "link": "",
    }

    lines = text.split("\n")

    # Текст отзыва — строки между "У Вас 1 новий відгук." и "Загальне враження:"
    review_lines = []
    in_review = False
    for line in lines:
        if "У Вас 1 новий відгук." in line:
            in_review = True
            continue
        if "Загальне враження:" in line:
            in_review = False
            # Парсим оценку
            if "Відмінно" in line:
                data["overall"] = "Відмінно"
            elif "Добре" in line:
                data["overall"] = "Добре"
            elif "Жахливо" in line:
                data["overall"] = "Жахливо"
            elif "Погано" in line:
                data["overall"] = "Погано"
            continue
        if in_review:
            review_lines.append(line)

    data["review_text"] = "\n".join(review_lines).strip()

    # Ссылка xpz.im
    link_match = re.search(r'https?://xpz\.im/\S+', text)
    if link_match:
        data["link"] = link_match.group(0)

    # Офіціант, Стіл, Сума
    waiter_match = re.search(r'Офіціант:\s*(.+)', text)
    if waiter_match:
        data["waiter"] = waiter_match.group(1).strip()

    table_match = re.search(r'Стіл:\s*(\d+)', text)
    if table_match:
        data["table"] = table_match.group(1).strip()

    # Оцінка страв
    in_dishes = False
    for line in lines:
        if "Оцінка страв" in line:
            in_dishes = True
            continue
        if in_dishes and line.strip():
            # Парсим строку типа: "Назва страви - 👍" или "Назва - 👎 (Коментар)" или "Назва - Без оцінки"
            if " - " in line:
                parts = line.split(" - ", 1)
                dish_name = parts[0].strip()
                rating_part = parts[1].strip() if len(parts) > 1 else ""

                if "👍" in rating_part:
                    rating = "positive"
                elif "👎" in rating_part:
                    rating = "negative"
                    # Извлекаем комментарий в скобках
                    comment_match = re.search(r'\((.+?)\)', rating_part)
                    if comment_match:
                        dish_name += f" ({comment_match.group(1)})"
                else:
                    rating = "none"

                data["dishes"].append({"name": dish_name, "rating": rating})

    return data

# ── Анализ текста через Claude ────────────────────────────────────────────────
def analyze_review(review_text: str, overall: str) -> dict:
    """
    Claude определяет есть ли замечание/пожелание в тексте.
    Возвращает {"has_complaint": bool, "summary": str}
    """
    # Жахливо/Погано — сразу негатив без запроса к Claude
    if overall in ("Жахливо", "Погано"):
        return {"has_complaint": True, "summary": review_text}

    if not review_text or len(review_text) < 5:
        return {"has_complaint": False, "summary": ""}

    try:
        prompt = f"""Ти аналізуєш відгук гостя ресторану.

Відгук: "{review_text}"
Загальна оцінка: {overall}

Визнач: є в тексті замечание, скарга, пожелание щось покращити, негативне порівняння?

Поверни ТІЛЬКИ валідний JSON без пояснень:
{{"has_complaint": true/false, "summary": "якщо є — коротко суть замечания, інакше пусто"}}"""

        response = claude_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}]
        )
        return json.loads(response.content[0].text.strip())
    except Exception as e:
        logger.error(f"Claude analyze error: {e}")
        return {"has_complaint": False, "summary": ""}

# ── Сохранение в Google Sheets ────────────────────────────────────────────────
async def save_review_to_sheets(data: dict, has_complaint: bool):
    try:
        gc = get_sheets_client()
        spreadsheet = gc.open_by_key(SPREADSHEET_ID)
        now = datetime.now()

        # Лист 1 — Відгуки
        sheet_reviews = get_or_create_sheet(spreadsheet, "Відгуки")
        ensure_headers(sheet_reviews, [
            "Дата", "Час", "Загальна оцінка", "Офіціант", "Стіл",
            "Текст відгуку", "Є замечання", "Посилання"
        ])
        sheet_reviews.insert_row([
            now.strftime("%d.%m.%Y"),
            now.strftime("%H:%M"),
            data["overall"],
            data["waiter"],
            data["table"],
            data["review_text"],
            "Так" if has_complaint else "Ні",
            data["link"],
        ], index=2)

        # Лист 2 — Рейтинг офіціантів
        if data["waiter"]:
            sheet_waiters = get_or_create_sheet(spreadsheet, "Рейтинг офіціантів")
            ensure_headers(sheet_waiters, [
                "Офіціант", "Відмінно", "Добре", "Погано", "Жахливо", "Всього", "% позитивних"
            ])
            all_waiters = sheet_waiters.get_all_records()
            waiter_row = next((i+2 for i, r in enumerate(all_waiters)
                               if r.get("Офіціант") == data["waiter"]), None)
            overall = data["overall"]
            if waiter_row:
                row_data = all_waiters[waiter_row - 2]
                vidminno = int(row_data.get("Відмінно", 0)) + (1 if overall == "Відмінно" else 0)
                dobre    = int(row_data.get("Добре", 0))    + (1 if overall == "Добре" else 0)
                pogano   = int(row_data.get("Погано", 0))   + (1 if overall == "Погано" else 0)
                zhakhlyvo= int(row_data.get("Жахливо", 0))  + (1 if overall == "Жахливо" else 0)
                total    = vidminno + dobre + pogano + zhakhlyvo
                pct      = round((vidminno + dobre) / total * 100) if total else 0
                sheet_waiters.update(f"B{waiter_row}:G{waiter_row}",
                    [[vidminno, dobre, pogano, zhakhlyvo, total, f"{pct}%"]])
            else:
                vidminno  = 1 if overall == "Відмінно" else 0
                dobre     = 1 if overall == "Добре" else 0
                pogano    = 1 if overall == "Погано" else 0
                zhakhlyvo = 1 if overall == "Жахливо" else 0
                total     = 1
                pct       = round((vidminno + dobre) / total * 100)
                sheet_waiters.append_row([
                    data["waiter"], vidminno, dobre, pogano, zhakhlyvo, total, f"{pct}%"
                ])

        # Лист 3 — Оцінка страв (только 👍 и 👎)
        negative_dishes = [d for d in data["dishes"] if d["rating"] == "negative"]
        positive_dishes = [d for d in data["dishes"] if d["rating"] == "positive"]

        if positive_dishes or negative_dishes:
            sheet_dishes = get_or_create_sheet(spreadsheet, "Оцінка страв")
            ensure_headers(sheet_dishes, [
                "Страва", "👍 Позитив", "👎 Негатив", "Всього згадувань"
            ])
            all_dishes = sheet_dishes.get_all_records()

            def update_dish(dish_name_raw, rating):
                # Убираем комментарий из названия для поиска
                clean_name = re.sub(r'\s*\(.*?\)', '', dish_name_raw).strip()
                dish_row = next((i+2 for i, r in enumerate(all_dishes)
                                 if r.get("Страва", "").lower() == clean_name.lower()), None)
                if dish_row:
                    row_data = all_dishes[dish_row - 2]
                    pos = int(row_data.get("👍 Позитив", 0)) + (1 if rating == "positive" else 0)
                    neg = int(row_data.get("👎 Негатив", 0)) + (1 if rating == "negative" else 0)
                    total = pos + neg
                    sheet_dishes.update(f"B{dish_row}:D{dish_row}", [[pos, neg, total]])
                else:
                    pos = 1 if rating == "positive" else 0
                    neg = 1 if rating == "negative" else 0
                    sheet_dishes.append_row([clean_name, pos, neg, 1])

            for d in positive_dishes:
                update_dish(d["name"], "positive")
            for d in negative_dishes:
                update_dish(d["name"], "negative")

        logger.info(f"Review saved: {data['overall']} / {data['waiter']} / complaint={has_complaint}")

    except Exception as e:
        logger.error(f"Sheets save error: {e}")

# ── Пересылка негатива в feedback бот ────────────────────────────────────────
async def forward_to_feedback_bot(data: dict, photo_file_ids: list, complaint_summary: str):
    """Отправляет негативный отзыв в feedback бот как повідомлення категории Гості."""
    try:
        feedback_bot = Bot(token=FEEDBACK_BOT_TOKEN)

        overall_icon = {
            "Відмінно": "😍", "Добре": "😊", "Погано": "😒", "Жахливо": "😡"
        }.get(data["overall"], "")

        negative_dishes = [d for d in data["dishes"] if d["rating"] == "negative"]
        dishes_text = ""
        if negative_dishes:
            dishes_text = "\n👎 Страви: " + ", ".join(d["name"] for d in negative_dishes)

        text = (
            f"🌿 *Терраса* / 💬 *Гості* / Expirenza\n\n"
            f"🗒️ {data['review_text']}"
            f"{dishes_text}\n\n"
            f"{overall_icon} {data['overall']} · 👤 {data['waiter']}"
            + (f"\n🔗 {data['link']}" if data['link'] else "")
        )

        # Генерируем feedback_id в стиле T-0104-G1
        now = datetime.now()
        feedback_id = f"T-{now.strftime('%d%m')}-G{now.strftime('%H%M%S')}"

        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        safe_id = feedback_id.replace("-", "_")
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Виконано", callback_data=f"status_done_{safe_id}"),
            InlineKeyboardButton("🔄 В роботі", callback_data=f"status_wip_{safe_id}"),
        ]])

        for uid in OWNER_IDS:
            try:
                await feedback_bot.send_message(
                    chat_id=uid, text=text,
                    parse_mode="Markdown", reply_markup=keyboard
                )
                for file_id in photo_file_ids:
                    await feedback_bot.send_photo(chat_id=uid, photo=file_id)
            except Exception as e:
                logger.error(f"Forward to feedback error {uid}: {e}")

        logger.info(f"Forwarded to feedback bot: {feedback_id}")

    except Exception as e:
        logger.error(f"Forward error: {e}")

# ── Обработчик входящих сообщений ────────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg:
        return

    # Временный лог для определения chat_id
    logger.info(f"DEBUG chat_id={update.effective_chat.id} | sender={msg.sender_chat.title if msg.sender_chat else (msg.from_user.full_name if msg.from_user else '?')}")

    # Только сообщения из нужного чата
    if REVIEWS_CHAT_ID and update.effective_chat.id != REVIEWS_CHAT_ID:
        return

    # Только сообщения от Expirenza бота (по имени отправителя)
    sender_name = ""
    if msg.forward_origin:
        pass
    if msg.from_user:
        sender_name = msg.from_user.full_name or ""
    elif msg.sender_chat:
        sender_name = msg.sender_chat.title or ""

    if EXPIRENZA_SENDER.lower() not in sender_name.lower():
        return

    text = msg.text or msg.caption or ""
    if not text:
        return

    # Парсим
    data = parse_expirenza_message(text)
    if not data:
        logger.info("Message skipped (not Моно-відгук)")
        return

    logger.info(f"Processing review: {data['overall']} / {data['waiter']}")

    # Анализируем на замечания
    analysis = analyze_review(data["review_text"], data["overall"])
    has_complaint = analysis.get("has_complaint", False)

    # Проверяем негативные блюда
    has_negative_dishes = any(d["rating"] == "negative" for d in data["dishes"])
    should_forward = has_complaint or has_negative_dishes

    # Фото если есть
    photo_file_ids = []
    if msg.photo:
        photo_file_ids.append(msg.photo[-1].file_id)

    # Сохраняем в таблицу
    await save_review_to_sheets(data, has_complaint)

    # Пересылаем в feedback бот если нужно
    if should_forward:
        await forward_to_feedback_bot(data, photo_file_ids, analysis.get("summary", ""))
        logger.info("Review forwarded to feedback bot")

# ── Запуск ────────────────────────────────────────────────────────────────────
def main():
    asyncio.set_event_loop(asyncio.new_event_loop())
    app = Application.builder().token(REVIEWS_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.ALL, handle_message))
    logger.info("reviews_expirenza бот запущено...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
