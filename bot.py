import logging
import os
import asyncio
import json
import gspread
from dotenv import load_dotenv
import pathlib
load_dotenv(dotenv_path=pathlib.Path(__file__).parent / ".env")
load_dotenv(dotenv_path=pathlib.Path(__file__).parent / "env")

from openai import AsyncOpenAI
import anthropic
from datetime import datetime
from google.oauth2.service_account import Credentials
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters, ConversationHandler
)

import tempfile
import re

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("FEEDBACK_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
SHEETS_CREDENTIALS = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
SPREADSHEET_ID = os.getenv("FEEDBACK_SPREADSHEET_ID")
OWNER_IDS = [int(x) for x in os.getenv("OWNER_IDS", "").split(",") if x]

RESTAURANTS = ["Терраса"]
ROLES = ["Офіціант", "Адміністратор", "Повар", "Шеф-повар", "Управляючий", "Виконавчий директор", "Технічний спеціаліст", "Клінінг"]
ADMIN_ROLES = {"Управляючий", "Виконавчий директор"}
KITCHEN_CATEGORIES = {"Кухня", "Закупки"}
CATEGORIES = ["Кухня", "Сервіс", "Техніка", "Закупки", "Гості", "Ідеї", "Чистота"]
CATEGORY_ICONS = {"Кухня": "🍽️", "Сервіс": "👥", "Техніка": "🔧", "Закупки": "🛒", "Гості": "💬", "Ідеї": "💡", "Чистота": "🧹"}

STATUS_PENDING = "pending"
STATUS_ACTIVE = "active"
STATUS_FIRED = "fired"

# ─── КОНСТАНТИ АВТОВИДАЛЕННЯ ──────────────────────────────────────────────────
TASK_ID_RE = re.compile(r'[A-Z]-\d{4}-\w+')   # T-0104-1 і подібні
AUTO_DELETE_DELAY = 30     # секунд — звичайні повідомлення
MENU_DELETE_DELAY = 120    # секунд — меню з кнопками
TASK_DELETE_DELAY = 86400  # секунд (24 год) — задачні повідомлення

GDRIVE_PHOTOS_PATH = r"G:\Мой диск\BOTS\Feedback\Фото"

ASK_FIRST_NAME, ASK_LAST_NAME, ASK_BIRTHDAY, ASK_PHONE, ASK_ROLE, ASK_RESTAURANT = range(6)

openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

PROFILES_FILE = os.path.join(os.path.dirname(__file__), "user_profiles.json")

def load_profiles():
    if os.path.exists(PROFILES_FILE):
        with open(PROFILES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return {int(k): v for k, v in data.items()}
    return {}

def save_profiles(profiles):
    with open(PROFILES_FILE, "w", encoding="utf-8") as f:
        json.dump({str(k): v for k, v in profiles.items()}, f, ensure_ascii=False, indent=2)

user_profiles = load_profiles()

# Хранилище message_id для редактирования уведомлений
MSG_STORE_FILE = os.path.join(os.path.dirname(__file__), "message_store.json")

def load_msg_store():
    if os.path.exists(MSG_STORE_FILE):
        with open(MSG_STORE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_msg_store(store):
    with open(MSG_STORE_FILE, "w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False, indent=2)

msg_store = load_msg_store()

# Хранилище назначений (доручити)
ASSIGN_STORE_FILE = os.path.join(os.path.dirname(__file__), "assign_store.json")

def load_assign_store():
    if os.path.exists(ASSIGN_STORE_FILE):
        with open(ASSIGN_STORE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_assign_store(store):
    with open(ASSIGN_STORE_FILE, "w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False, indent=2)

assign_store = load_assign_store()

# ─── ПРОМПТ ───────────────────────────────────────────────────────────────────

PROMPT_FILE = os.path.join(os.path.dirname(__file__), "prompt.json")

DEFAULT_PROMPT = """Ти помічник для аналізу повідомлень персоналу ресторану.
Заклад: <<restaurant>>
<<role_line>>Повідомлення: "<<message_text>>"

Якщо в повідомленні є кілька різних проблем — поверни масив об'єктів. Якщо одна — масив з одного об'єкта.

Поверни ТІЛЬКИ валідний JSON без пояснень та без markdown:
[{"category": "Кухня|Сервіс|Техніка|Закупки|Гості|Ідеї|Чистота",
  "summary": "текст повідомлення як є, виправ лише явні описки та заїкання",
  "responsible": "Шеф-повар|Управляючий|Виконавчий директор|Власник",
  "urgency": "Висока|Стандартна|Низька"}]

Правила summary:
- Зберігай оригінальний текст максимально близько до того, як написав співробітник
- Виправляй лише явні описки, повтори слів, заїкання
- НЕ перефразовуй, НЕ скорочуй, НЕ інтерпретуй
- Поганий приклад: "Бруд на підлозі" (скорочено і переінтерпретовано)
- Гарний приклад: "в залі брудна підлога біля четвертого столика, гості скаржаться" (як сказав співробітник)

Правила urgency:
- Техніка зламалась → Висока
- Скарга гостя → Висока
- Ідея → Низька
- Решта → Стандартна

Категорія Чистота: бруд, прибирання, гігієна"""

def load_prompt():
    if os.path.exists(PROMPT_FILE):
        with open(PROMPT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("template", DEFAULT_PROMPT)
    return DEFAULT_PROMPT

def save_prompt(template):
    with open(PROMPT_FILE, "w", encoding="utf-8") as f:
        json.dump({"template": template}, f, ensure_ascii=False, indent=2)

# ─── ТРЕКІНГ ПОВІДОМЛЕНЬ (для очищення перед дайджестом) ─────────────────────

CHAT_MSGS_FILE = os.path.join(os.path.dirname(__file__), "chat_msgs.json")

def load_chat_msgs():
    if os.path.exists(CHAT_MSGS_FILE):
        with open(CHAT_MSGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_chat_msgs_data(data):
    with open(CHAT_MSGS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f)

chat_msgs_store = load_chat_msgs()

def track_msg(chat_id, message_id):
    """Відстежує не-задачне повідомлення бота для очищення перед дайджестом."""
    key = str(chat_id)
    if key not in chat_msgs_store:
        chat_msgs_store[key] = []
    if message_id not in chat_msgs_store[key]:
        chat_msgs_store[key].append(message_id)
    save_chat_msgs_data(chat_msgs_store)

async def delete_tracked_messages(bot, chat_ids):
    """Видаляє всі відстежені (не-задачні) повідомлення бота перед дайджестом."""
    for chat_id in chat_ids:
        key = str(chat_id)
        for mid in list(chat_msgs_store.get(key, [])):
            try:
                await bot.delete_message(chat_id=chat_id, message_id=mid)
            except Exception:
                pass
        chat_msgs_store[key] = []
    save_chat_msgs_data(chat_msgs_store)


def build_task_keyboard(safe_id, assigned=False, done=False):
    """Строит клавиатуру для уведомления о задаче."""
    if done:
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("↩️", callback_data=f"status_cancel_{safe_id}"),
            InlineKeyboardButton("💬", callback_data=f"comment_{safe_id}"),
            InlineKeyboardButton("🗑️", callback_data=f"status_del_{safe_id}"),
        ]])
    assign_btn = InlineKeyboardButton("🔁", callback_data=f"assign_{safe_id}") if assigned \
                 else InlineKeyboardButton("👤", callback_data=f"assign_{safe_id}")
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅", callback_data=f"status_done_{safe_id}"),
        InlineKeyboardButton("🔧", callback_data=f"status_wip_{safe_id}"),
        assign_btn,
        InlineKeyboardButton("💬", callback_data=f"comment_{safe_id}"),
        InlineKeyboardButton("🗑️", callback_data=f"status_del_{safe_id}"),
    ]])

def build_wip_keyboard(safe_id, assigned=False):
    assign_btn = InlineKeyboardButton("🔁", callback_data=f"assign_{safe_id}") if assigned \
                 else InlineKeyboardButton("👤", callback_data=f"assign_{safe_id}")
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅", callback_data=f"status_done_{safe_id}"),
        InlineKeyboardButton("↩️", callback_data=f"status_cancel_{safe_id}"),
        assign_btn,
        InlineKeyboardButton("💬", callback_data=f"comment_{safe_id}"),
        InlineKeyboardButton("🗑️", callback_data=f"status_del_{safe_id}"),
    ]])

def get_recipients(category=None):
    """Динамически получает получателей из профилей по роли."""
    recipients = set(OWNER_IDS)
    for uid, p in user_profiles.items():
        if p.get("status") != STATUS_ACTIVE:
            continue
        role = p.get("role", "")
        if role in ("Управляючий", "Виконавчий директор", "Адміністратор"):
            recipients.add(uid)
        elif role == "Шеф-повар" and category in KITCHEN_CATEGORIES:
            recipients.add(uid)
    return recipients

def is_admin(user_id):
    if user_id in OWNER_IDS:
        return True
    profile = user_profiles.get(user_id, {})
    return profile.get("role") in ADMIN_ROLES and profile.get("status") == STATUS_ACTIVE

def can_manage_tasks(user_id):
    """Права на кнопки задач: власник + Управляючий + Виконавчий директор + Адміністратор."""
    if user_id in OWNER_IDS:
        return True
    profile = user_profiles.get(user_id, {})
    return profile.get("role") in {"Управляючий", "Виконавчий директор", "Адміністратор"} \
           and profile.get("status") == STATUS_ACTIVE

def is_owner(user_id):
    return user_id in OWNER_IDS

def get_display_name(profile):
    return f"{profile.get('first_name', '')} {profile.get('last_name', '')}".strip()

def get_sheets_client():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(SHEETS_CREDENTIALS, scopes=scopes)
    return gspread.authorize(creds)

def get_main_keyboard(user_id):
    """Возвращает Reply-клавиатуру в зависимости от роли."""
    buttons = [[KeyboardButton("💬 Надіслати повідомлення")]]
    if is_admin(user_id):
        buttons.append([KeyboardButton("👨‍💼 Адмін-меню")])
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)


# ─── УТИЛІТА: автовидалення сервісних повідомлень ───────────────────────────

async def safe_answer(query, text=None, show_alert=False):
    """Відповідає на callback, ігноруючи помилку застарілого запиту."""
    try:
        if text:
            await query.answer(text=text, show_alert=show_alert)
        else:
            await query.answer()
    except Exception:
        pass

def schedule_delete(context, bot, chat_id, message_id, delay, job_name=None):
    """Планує видалення повідомлення через delay секунд."""
    async def _delete(ctx):
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception:
            pass
    context.job_queue.run_once(_delete, when=delay, name=job_name)


def cancel_user_jobs(context, user_id, job_prefix):
    """Отменяет все задачи пользователя с заданным префиксом."""
    jobs = context.job_queue.get_jobs_by_name(f"{job_prefix}_{user_id}")
    for job in jobs:
        job.schedule_removal()


# ─── РЕЄСТРАЦІЯ ──────────────────────────────────────────────────────────────

async def start(update, context):
    user_id = update.effective_user.id
    if user_id in user_profiles:
        profile = user_profiles[user_id]
        status = profile.get("status")
        if status == STATUS_PENDING:
            await update.message.reply_text("⏳ Ваша заявка на реєстрацію очікує підтвердження від керівника. Ми повідомимо вас як тільки її розглянуть.")
            return ConversationHandler.END
        if status == STATUS_FIRED:
            await update.message.reply_text("❌ Ваш доступ відхилено. Зверніться до керівника.")
            return ConversationHandler.END
        if status == STATUS_ACTIVE:
            name = get_display_name(profile)
            await update.message.reply_text(
                f"З поверненням, {name}! 👋\n\n"
                f"🏠 Заклад: {profile.get('restaurant', '—')}\n"
                f"💼 Роль: {profile.get('role', '—')}\n\n"
                "Надішліть повідомлення або скористайтесь кнопками.",
                reply_markup=get_main_keyboard(user_id)
            )
            return ConversationHandler.END
    await update.message.reply_text("Вітаю! 👋 Це бот для зворотного зв'язку персоналу.\n\nВведіть ваше *ім'я*:", parse_mode="Markdown")
    return ASK_FIRST_NAME

async def ask_first_name(update, context):
    context.user_data['first_name'] = update.message.text.strip()
    await update.message.reply_text("Введіть ваше *прізвище*:", parse_mode="Markdown")
    return ASK_LAST_NAME

async def ask_last_name(update, context):
    context.user_data['last_name'] = update.message.text.strip()
    await update.message.reply_text("Введіть вашу *дату народження* у форматі ДД.ММ.РРРР\nНаприклад: 15.03.1990", parse_mode="Markdown")
    return ASK_BIRTHDAY

async def ask_birthday(update, context):
    text = update.message.text.strip()
    try:
        datetime.strptime(text, "%d.%m.%Y")
        context.user_data['birthday'] = text
    except ValueError:
        await update.message.reply_text("❌ Невірний формат. Введіть дату: ДД.ММ.РРРР")
        return ASK_BIRTHDAY
    keyboard = ReplyKeyboardMarkup([[KeyboardButton("📱 Поділитися номером", request_contact=True)]], resize_keyboard=True, one_time_keyboard=True)
    await update.message.reply_text("Поділіться вашим номером телефону або введіть вручну:", reply_markup=keyboard)
    return ASK_PHONE

async def ask_phone(update, context):
    if update.message.contact:
        context.user_data['phone'] = update.message.contact.phone_number
    else:
        context.user_data['phone'] = update.message.text.strip()
    keyboard = [[InlineKeyboardButton(role, callback_data=f"reg_role_{role}")] for role in ROLES]
    await update.message.reply_text("Оберіть вашу *роль*:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return ASK_ROLE

async def ask_role_reg(update, context):
    query = update.callback_query
    await safe_answer(query)
    context.user_data['role'] = query.data.replace("reg_role_", "")
    keyboard = [[InlineKeyboardButton(r, callback_data=f"reg_rest_{r}")] for r in RESTAURANTS]
    await query.edit_message_text("Оберіть ваш *заклад*:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return ASK_RESTAURANT

async def ask_restaurant_reg(update, context):
    query = update.callback_query
    await safe_answer(query)
    context.user_data['restaurant'] = query.data.replace("reg_rest_", "")
    user_id = update.effective_user.id
    d = context.user_data
    user_profiles[user_id] = {
        "first_name": d.get('first_name'),
        "last_name": d.get('last_name'),
        "birthday": d.get('birthday'),
        "phone": d.get('phone'),
        "role": d.get('role'),
        "restaurant": d.get('restaurant'),
        "status": STATUS_PENDING,
        "telegram_id": user_id,
        "registered_at": datetime.now().strftime("%d.%m.%Y %H:%M"),
    }
    save_profiles(user_profiles)
    full_name = f"{d.get('first_name')} {d.get('last_name')}"
    await query.edit_message_text(
        f"✅ Заявку подано!\n\n👤 {full_name}\n💼 {d.get('role')}\n🏠 {d.get('restaurant')}\n\n"
        "⏳ Очікуйте підтвердження від керівника."
    )
    await notify_admins_new_registration(user_id, user_profiles[user_id])
    return ConversationHandler.END

async def notify_admins_new_registration(user_id, profile):
    from telegram import Bot
    bot = Bot(token=BOT_TOKEN)
    full_name = get_display_name(profile)
    text = (
        f"🆕 *Нова заявка на реєстрацію*\n\n"
        f"👤 {full_name}\n💼 {profile.get('role')}\n🏠 {profile.get('restaurant')}\n"
        f"📱 {profile.get('phone', '—')}\n🎂 {profile.get('birthday', '—')}\n📅 {profile.get('start_date', '—')}"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Підтвердити", callback_data=f"approve_{user_id}"),
        InlineKeyboardButton("❌ Відхилити", callback_data=f"reject_{user_id}"),
    ]])
    for admin_id in get_recipients():
        try:
            await bot.send_message(chat_id=admin_id, text=text, reply_markup=keyboard, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Notify admin error {admin_id}: {e}")

async def handle_approve(update, context):
    query = update.callback_query
    await safe_answer(query)
    if not is_admin(update.effective_user.id):
        await safe_answer(query, "Немає прав.", show_alert=True)
        return
    user_id = int(query.data.replace("approve_", ""))
    if user_id not in user_profiles:
        await query.edit_message_text("❌ Користувача не знайдено.")
        return
    user_profiles[user_id]["status"] = STATUS_ACTIVE
    save_profiles(user_profiles)
    profile = user_profiles[user_id]
    full_name = get_display_name(profile)
    await query.edit_message_text(f"✅ {full_name} — підтверджено!")
    from telegram import Bot
    bot = Bot(token=BOT_TOKEN)
    try:
        await bot.send_message(chat_id=user_id, text=f"✅ Вашу реєстрацію підтверджено!\n\nЛаскаво просимо, {profile.get('first_name')}! 🎉\nТепер ви можете надсилати повідомлення.", reply_markup=get_main_keyboard(user_id))
    except Exception as e:
        logger.error(f"Approve notify error: {e}")

async def handle_reject(update, context):
    query = update.callback_query
    await safe_answer(query)
    if not is_admin(update.effective_user.id):
        await safe_answer(query, "Немає прав.", show_alert=True)
        return
    user_id = int(query.data.replace("reject_", ""))
    if user_id not in user_profiles:
        await query.edit_message_text("❌ Користувача не знайдено.")
        return
    user_profiles[user_id]["status"] = STATUS_FIRED
    save_profiles(user_profiles)
    full_name = get_display_name(user_profiles[user_id])
    await query.edit_message_text(f"❌ {full_name} — відхилено.")
    from telegram import Bot
    bot = Bot(token=BOT_TOKEN)
    try:
        await bot.send_message(chat_id=user_id, text="❌ Вашу заявку на реєстрацію відхилено. Зверніться до керівника.")
    except Exception as e:
        logger.error(f"Reject notify error: {e}")


# ─── ОТРИМАННЯ ПОВІДОМЛЕНЬ ────────────────────────────────────────────────────

async def receive_text(update, context):
    user_id = update.effective_user.id
    text = update.message.text

    # Автовидалення повідомлення користувача
    from telegram import Bot as _Bot
    _bot = _Bot(token=BOT_TOKEN)
    schedule_delete(context, _bot, update.effective_chat.id, update.message.message_id, delay=AUTO_DELETE_DELAY)

    if text == "👨‍💼 Адмін-меню":
        if is_admin(user_id):
            await show_admin_menu(update, context)
        else:
            msg = await update.message.reply_text("❌ Немає прав.")
            track_msg(msg.chat_id, msg.message_id)
            schedule_delete(context, _bot, msg.chat_id, msg.message_id, delay=AUTO_DELETE_DELAY)
        return

    if text == "💬 Надіслати повідомлення":
        msg = await update.message.reply_text("Надішліть текстове або голосове повідомлення 👇")
        track_msg(msg.chat_id, msg.message_id)
        schedule_delete(context, _bot, msg.chat_id, msg.message_id, delay=AUTO_DELETE_DELAY)
        return

    if user_id not in user_profiles:
        msg = await update.message.reply_text("Спочатку зареєструйтесь: /start")
        track_msg(msg.chat_id, msg.message_id)
        schedule_delete(context, _bot, msg.chat_id, msg.message_id, delay=AUTO_DELETE_DELAY)
        return
    status = user_profiles[user_id].get("status")
    if status == STATUS_PENDING:
        msg = await update.message.reply_text("⏳ Ваша заявка ще на розгляді. Очікуйте підтвердження.")
        track_msg(msg.chat_id, msg.message_id)
        schedule_delete(context, _bot, msg.chat_id, msg.message_id, delay=AUTO_DELETE_DELAY)
        return
    if status == STATUS_FIRED:
        msg = await update.message.reply_text("❌ Ваш доступ відхилено.")
        track_msg(msg.chat_id, msg.message_id)
        schedule_delete(context, _bot, msg.chat_id, msg.message_id, delay=AUTO_DELETE_DELAY)
        return

    # Режим редагування промпту — тільки голосові, текст ігноруємо
    if context.user_data.get('prompt_editing') and is_owner(user_id):
        msg = await update.message.reply_text("🎙 Для редагування промпту надішліть *голосове* повідомлення.", parse_mode="Markdown")
        track_msg(msg.chat_id, msg.message_id)
        schedule_delete(context, _bot, msg.chat_id, msg.message_id, delay=AUTO_DELETE_DELAY)
        return

    # Режим комментария
    if context.user_data.get('commenting_on'):
        handled = await process_comment(update, context)
        if handled:
            return

    cancel_user_jobs(context, user_id, "photoclear")
    context.user_data['pending_message'] = update.message.text
    context.user_data['message_type'] = 'text'
    await ask_send_options(update, context)

async def receive_voice(update, context):
    user_id = update.effective_user.id

    # Автовидалення голосового повідомлення користувача
    from telegram import Bot as _Bot
    _bot = _Bot(token=BOT_TOKEN)
    schedule_delete(context, _bot, update.effective_chat.id, update.message.message_id, delay=AUTO_DELETE_DELAY)

    # Режим редагування промпту (тільки для власника)
    if context.user_data.get('prompt_editing') and is_owner(user_id):
        await process_prompt_voice(update, context)
        return

    if user_id not in user_profiles:
        msg = await update.message.reply_text("Спочатку зареєструйтесь: /start")
        track_msg(msg.chat_id, msg.message_id)
        schedule_delete(context, _bot, msg.chat_id, msg.message_id, delay=AUTO_DELETE_DELAY)
        return
    status = user_profiles[user_id].get("status")
    if status == STATUS_PENDING:
        msg = await update.message.reply_text("⏳ Ваша заявка ще на розгляді.")
        track_msg(msg.chat_id, msg.message_id)
        schedule_delete(context, _bot, msg.chat_id, msg.message_id, delay=AUTO_DELETE_DELAY)
        return
    if status == STATUS_FIRED:
        msg = await update.message.reply_text("❌ Ваш доступ відхилено.")
        track_msg(msg.chat_id, msg.message_id)
        schedule_delete(context, _bot, msg.chat_id, msg.message_id, delay=AUTO_DELETE_DELAY)
        return
    processing_msg = await update.message.reply_text("🎙 Голосове отримано, обробляю...")
    track_msg(processing_msg.chat_id, processing_msg.message_id)
    schedule_delete(context, _bot, processing_msg.chat_id, processing_msg.message_id, delay=AUTO_DELETE_DELAY)
    voice_file = await update.message.voice.get_file()
    file_path = os.path.join(tempfile.gettempdir(), f"voice_{user_id}.ogg")
    await voice_file.download_to_drive(file_path)
    context.user_data['pending_message'] = file_path
    context.user_data['message_type'] = 'voice'
    cancel_user_jobs(context, user_id, "photoclear")
    await ask_send_options(update, context)


async def ask_send_options(update, context):
    """Показывает 3 кнопки и запускает 20-секундный таймер автоотправки."""
    user_id = update.effective_user.id
    cancel_user_jobs(context, user_id, "autosend")

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("👤 Відправити", callback_data="send_named")],
        [InlineKeyboardButton("🕵️ Анонімно", callback_data="send_anon")],
        [InlineKeyboardButton("📷 Додати фото", callback_data="send_photo")],
    ])
    msg = await update.message.reply_text(
        "⏱ Автовідправка через 20 сек від вашого імені.\nАбо оберіть варіант:",
        reply_markup=keyboard
    )
    context.user_data['option_message_id'] = msg.message_id
    context.user_data['option_chat_id'] = msg.chat_id
    context.user_data['waiting_for_option'] = True

    from telegram import Bot
    bot = Bot(token=BOT_TOKEN)
    # Видаляємо через 25 сек (трохи більше таймера автовідправки)
    schedule_delete(context, bot, msg.chat_id, msg.message_id, delay=25,
                    job_name=f"optdelete_{user_id}")

    context.job_queue.run_once(
        auto_send_callback,
        when=20,
        name=f"autosend_{user_id}",
        data={"user_id": user_id, "chat_id": update.effective_chat.id}
    )

async def auto_send_callback(context):
    """Автоматически отправляет сообщение от имени пользователя через 20 сек."""
    user_id = context.job.data["user_id"]
    chat_id = context.job.data["chat_id"]
    user_data = context.application.user_data.get(user_id, {})

    if not user_data.get('waiting_for_option'):
        return

    profile = user_profiles.get(user_id, {})
    user_data['sender_name'] = get_display_name(profile)
    user_data['sender_role'] = profile.get('role', '—')
    user_data['is_anonymous'] = False
    user_data['pending_photo'] = user_data.get('pending_photo')
    user_data['waiting_for_option'] = False

    from telegram import Bot
    bot = Bot(token=BOT_TOKEN)

    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=user_data.get('option_message_id'),
            text="⏳ Аналізую повідомлення..."
        )
    except Exception:
        pass

    await _do_process(bot, chat_id, user_id, user_data, profile, context)

async def handle_send_option(update, context):
    """Обработчик кнопок: Відправити / Анонімно / Фото."""
    query = update.callback_query
    await safe_answer(query)
    user_id = update.effective_user.id

    cancel_user_jobs(context, user_id, "autosend")
    cancel_user_jobs(context, user_id, "optdelete")
    context.user_data['waiting_for_option'] = False

    profile = user_profiles.get(user_id, {})
    action = query.data

    from telegram import Bot as _Bot
    _bot = _Bot(token=BOT_TOKEN)

    if action == "send_named":
        context.user_data['sender_name'] = get_display_name(profile)
        context.user_data['sender_role'] = profile.get('role', '—')
        context.user_data['is_anonymous'] = False
        await query.edit_message_text("⏳ Аналізую повідомлення...")
        schedule_delete(context, _bot, query.message.chat_id, query.message.message_id, delay=60)
        await process_message(query, context, profile)

    elif action == "send_anon":
        context.user_data['sender_name'] = "Аноним"
        context.user_data['sender_role'] = "—"
        context.user_data['is_anonymous'] = True
        await query.edit_message_text("⏳ Аналізую повідомлення...")
        schedule_delete(context, _bot, query.message.chat_id, query.message.message_id, delay=60)
        await process_message(query, context, profile)

    elif action == "send_photo":
        context.user_data['waiting_for_photo'] = True
        context.user_data['sender_name'] = get_display_name(profile)
        context.user_data['sender_role'] = profile.get('role', '—')
        context.user_data['is_anonymous'] = False
        await query.edit_message_text("📷 Надішліть фото — і повідомлення відправиться автоматично.")
        schedule_delete(context, _bot, query.message.chat_id, query.message.message_id, delay=MENU_DELETE_DELAY)


async def receive_photo(update, context):
    """Обработчик входящих фото."""
    user_id = update.effective_user.id

    if user_id not in user_profiles or user_profiles[user_id].get("status") != STATUS_ACTIVE:
        return

    # Автовидалення фото користувача
    from telegram import Bot as _Bot
    _bot = _Bot(token=BOT_TOKEN)
    schedule_delete(context, _bot, update.effective_chat.id, update.message.message_id, delay=AUTO_DELETE_DELAY)

    photo_file_id = update.message.photo[-1].file_id

    # Сценарий А: ждём фото после нажатия кнопки "Додати фото"
    if context.user_data.get('waiting_for_photo') and context.user_data.get('pending_message'):
        context.user_data['pending_photo'] = photo_file_id
        context.user_data['waiting_for_photo'] = False
        profile = user_profiles.get(user_id, {})
        await update.message.reply_text("⏳ Аналізую повідомлення...")
        await process_message(update, context, profile, use_message=True)
        return

    # Сценарий Б: фото пришло до текста — держим 2 минуты
    if 'pending_message' not in context.user_data:
        cancel_user_jobs(context, user_id, "photoclear")
        context.user_data['pending_photo'] = photo_file_id
        sent = await update.message.reply_text(
            "📷 Фото збережено! Тепер напишіть або надиктуйте що сталось.\n"
            "_Якщо нічого не надійде протягом 2 хв — фото буде скинуто._",
            parse_mode="Markdown"
        )
        from telegram import Bot
        bot = Bot(token=BOT_TOKEN)
        # Видаляємо підказку разом із таймаутом фото
        schedule_delete(context, bot, sent.chat_id, sent.message_id, delay=120,
                        job_name=f"photomsgdelete_{user_id}")
        context.job_queue.run_once(
            photo_clear_callback,
            when=120,
            name=f"photoclear_{user_id}",
            data={"user_id": user_id, "chat_id": update.effective_chat.id}
        )
        return

    await update.message.reply_text("Спочатку надішліть текстове або голосове повідомлення.")

async def photo_clear_callback(context):
    """Сбрасывает фото если текст не пришёл за 2 минуты."""
    user_id = context.job.data["user_id"]
    chat_id = context.job.data["chat_id"]
    user_data = context.application.user_data.get(user_id, {})
    if 'pending_photo' in user_data and 'pending_message' not in user_data:
        user_data.pop('pending_photo', None)
        from telegram import Bot
        bot = Bot(token=BOT_TOKEN)
        try:
            sent = await bot.send_message(chat_id=chat_id, text="⏱ Час вийшов. Фото скинуто. Надішліть повідомлення знову.")
            # Видаляємо системне повідомлення через 30 сек
            schedule_delete(context, bot, chat_id, sent.message_id, delay=30)
        except Exception:
            pass


GDRIVE_PHOTOS_PATH = r"G:\Мой диск\BOTS\Feedback\Фото"

async def save_photo_to_gdrive(file_id: str, filename: str) -> str:
    """Скачивает фото из Telegram и сохраняет в папку с датой."""
    try:
        from telegram import Bot
        bot = Bot(token=BOT_TOKEN)
        tg_file = await bot.get_file(file_id)

        today_folder = os.path.join(GDRIVE_PHOTOS_PATH, datetime.now().strftime("%Y-%m-%d"))
        os.makedirs(today_folder, exist_ok=True)
        dest_path = os.path.join(today_folder, filename)
        await tg_file.download_to_drive(dest_path)

        logger.info(f"Photo saved to: {dest_path}")
        return dest_path
    except Exception as e:
        logger.error(f"Photo save error: {e}")
        return ""


async def process_message(query_or_update, context, profile, use_message=False):
    message_type = context.user_data.get('message_type')
    pending = context.user_data.get('pending_message')
    if message_type == 'voice':
        text = await transcribe_voice(pending)
    else:
        text = pending
    if not text:
        target = query_or_update.message
        await target.reply_text("❌ Не вдалося обробити повідомлення.")
        return

    photo_file_id = context.user_data.pop('pending_photo', None)
    is_anonymous = context.user_data.get('is_anonymous', False)
    now = datetime.now()

    if photo_file_id:
        filename = f"feedback_{now.strftime('%Y%m%d_%H%M%S')}.jpg"
        await save_photo_to_gdrive(photo_file_id, filename)

    results = await analyze_with_claude(text, profile, is_anonymous=is_anonymous)

    categories = []
    for i, result in enumerate(results):
        photo = photo_file_id if i == 0 else None
        feedback_id = await save_to_sheets(result, context.user_data, profile, has_photos=photo is not None)
        await send_notifications(result, context.user_data, profile, photo, feedback_id, context=context)
        categories.append(result.get('category', '—'))

    for key in ['pending_message', 'message_type', 'pending_photo', 'waiting_for_option', 'waiting_for_photo', 'option_message_id', 'option_chat_id']:
        context.user_data.pop(key, None)

    anon_note = " (анонімно)" if is_anonymous else ""
    photo_note = "\n📷 З фото" if photo_file_id else ""
    count_note = f"\n📊 Знайдено проблем: {len(results)}" if len(results) > 1 else ""
    cats = ", ".join(categories)

    target_msg = query_or_update.message
    sent = await target_msg.reply_text(
        f"✅ Повідомлення надіслано{anon_note}!\n\n"
        f"📂 Категорії: {cats}{count_note}{photo_note}\n\nДякуємо за зворотний зв'язок 🙏"
    )
    from telegram import Bot
    bot = Bot(token=BOT_TOKEN)
    # Видаляємо підтвердження через 90 сек
    schedule_delete(context, bot, sent.chat_id, sent.message_id, delay=90)

async def _do_process(bot, chat_id, user_id, user_data, profile, context=None):
    """Вспомогательная функция для автоотправки (без query объекта)."""
    message_type = user_data.get('message_type')
    pending = user_data.get('pending_message')
    if message_type == 'voice':
        text = await transcribe_voice(pending)
    else:
        text = pending
    if not text:
        await bot.send_message(chat_id=chat_id, text="❌ Не вдалося обробити повідомлення.")
        return

    photo_file_id = user_data.pop('pending_photo', None)
    is_anonymous = user_data.get('is_anonymous', False)
    now = datetime.now()

    if photo_file_id:
        filename = f"feedback_{now.strftime('%Y%m%d_%H%M%S')}.jpg"
        await save_photo_to_gdrive(photo_file_id, filename)

    results = await analyze_with_claude(text, profile, is_anonymous=is_anonymous)

    categories = []
    for i, result in enumerate(results):
        photo = photo_file_id if i == 0 else None
        feedback_id = await save_to_sheets(result, user_data, profile, has_photos=photo is not None)
        await send_notifications(result, user_data, profile, photo, feedback_id, context=context)
        categories.append(result.get('category', '—'))

    for key in ['pending_message', 'message_type', 'pending_photo', 'waiting_for_option', 'waiting_for_photo']:
        user_data.pop(key, None)

    anon_note = " (анонімно)" if is_anonymous else ""
    photo_note = "\n📷 З фото" if photo_file_id else ""
    cats = ", ".join(categories)
    count_note = f"\n📊 Знайдено проблем: {len(results)}" if len(results) > 1 else ""

    sent = await bot.send_message(
        chat_id=chat_id,
        text=f"✅ Повідомлення надіслано{anon_note}!\n\n📂 Категорії: {cats}{count_note}{photo_note}\n\nДякуємо за зворотний зв'язок 🙏"
    )
    if context:
        # Видаляємо підтвердження через 90 сек
        schedule_delete(context, bot, chat_id, sent.message_id, delay=90)


# ─── ГОЛОС / ТРАНСКРИПЦІЯ ─────────────────────────────────────────────────────

async def transcribe_voice(file_path):
    try:
        with open(file_path, "rb") as audio_file:
            response = await openai_client.audio.transcriptions.create(model="whisper-1", file=audio_file, language=None)
        return response.text
    except Exception as e:
        logger.error(f"Whisper error: {e}")
        return None
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)

async def analyze_with_claude(text, profile, is_anonymous=False):
    try:
        role_line = "" if is_anonymous else f"Роль відправника: {profile.get('role', 'Невідомо')}\n"
        template = load_prompt()
        prompt = (template
            .replace("<<restaurant>>", profile.get('restaurant', 'Невідомо'))
            .replace("<<role_line>>", role_line)
            .replace("<<message_text>>", text))

        response = claude_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = response.content[0].text.strip()
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            parsed = [parsed]
        return parsed

    except Exception as e:
        logger.error(f"Claude error: {e}")
        return [{"category": "Інше", "summary": text[:50], "action": "Переглянути вручну", "responsible": "Управляючий", "urgency": "Стандартна"}]


# ─── GOOGLE SHEETS ────────────────────────────────────────────────────────────

def get_msg_data(feedback_id):
    """Повертає (msg_ids dict, original_text) з msg_store."""
    data = msg_store.get(feedback_id, {})
    if isinstance(data, dict) and "ids" in data:
        return data["ids"], data.get("text", "")
    return data, ""

async def edit_all_messages(bot, feedback_id, new_text, keyboard):
    """Редагує повідомлення у всіх отримувачів і оновлює збережений текст."""
    msg_ids, _ = get_msg_data(feedback_id)
    for uid_str, message_id in msg_ids.items():
        # Підтримуємо як один id (int), так і список [id1, id2, ...]
        ids = message_id if isinstance(message_id, list) else [message_id]
        for mid in ids:
            try:
                await bot.edit_message_text(
                    chat_id=int(uid_str),
                    message_id=mid,
                    text=new_text,
                    parse_mode="Markdown",
                    reply_markup=keyboard
                )
            except Exception as e:
                logger.error(f"Edit message error {uid_str}/{mid}: {e}")
    if feedback_id in msg_store and isinstance(msg_store[feedback_id], dict):
        msg_store[feedback_id]["text"] = new_text
        save_msg_store(msg_store)

def get_restaurant_prefix(restaurant):
    prefixes = {"Терраса": "T", "Хочу": "H", "Хочу 2.0": "H2"}
    return prefixes.get(restaurant, "X")

def generate_feedback_id(sheet, restaurant):
    """Генерирует ID вида T-0104-1, T-0104-2..."""
    prefix = get_restaurant_prefix(restaurant)
    today = datetime.now().strftime("%d%m")
    id_prefix = f"{prefix}-{today}-"
    try:
        all_ids = sheet.col_values(1)
        today_ids = [v for v in all_ids if v.startswith(id_prefix)]
        next_num = len(today_ids) + 1
    except Exception:
        next_num = 1
    return f"{id_prefix}{next_num}"

async def save_to_sheets(result, user_data, profile, has_photos=False):
    try:
        gc = get_sheets_client()
        spreadsheet = gc.open_by_key(SPREADSHEET_ID)
        try:
            sheet = spreadsheet.worksheet("Зворотний зв'язок")
        except gspread.WorksheetNotFound:
            sheet = spreadsheet.add_worksheet("Зворотний зв'язок", rows=1000, cols=13)
            sheet.append_row(["Номер", "Дата", "Час", "Заклад", "Хто повідомив", "Роль", "Категорія", "Суть", "Відповідальний", "Терміновість", "Фото", "Статус", "Лог"])

        now = datetime.now()
        feedback_id = generate_feedback_id(sheet, profile.get("restaurant", "X"))

        row = [
            feedback_id,
            now.strftime("%d.%m.%Y"), now.strftime("%H:%M"),
            profile.get("restaurant", "—"), user_data.get("sender_name", "—"), user_data.get("sender_role", "—"),
            result.get("category", "—"), result.get("summary", "—"),
            result.get("responsible", "—"), result.get("urgency", "—"),
            "є фото" if has_photos else "—", "Нове", "Нове"
        ]
        sheet.insert_row(row, index=2)
        logger.info(f"Saved to Sheets ID {feedback_id}: {result.get('category')} / {result.get('urgency')}")
        return feedback_id
    except Exception as e:
        logger.error(f"Sheets error: {e}")
        return None

async def update_sheet_history(feedback_id, entry):
    """Додає запис в колонку Історія (col 13)."""
    try:
        gc = get_sheets_client()
        sheet = gc.open_by_key(SPREADSHEET_ID).worksheet("Зворотний зв'язок")
        all_ids = sheet.col_values(1)
        for i, val in enumerate(all_ids):
            if val == feedback_id:
                current = sheet.cell(i + 1, 13).value or ""
                new_history = (current + " | " + entry).strip(" | ")
                sheet.update_cell(i + 1, 13, new_history)
                return
    except Exception as e:
        logger.error(f"History update error: {e}")

async def update_sheet_status(feedback_id, status, who):
    """Обновляет статус строки в Google Sheets по ID."""
    try:
        gc = get_sheets_client()
        sheet = gc.open_by_key(SPREADSHEET_ID).worksheet("Зворотний зв'язок")
        all_ids = sheet.col_values(1)
        for i, val in enumerate(all_ids):
            if val == feedback_id:
                sheet.update_cell(i + 1, 12, f"{status}" + (f" — {who}" if who else ""))
                logger.info(f"Status updated ID {feedback_id}: {status}")
                return
        logger.warning(f"ID {feedback_id} not found in sheet")
    except Exception as e:
        logger.error(f"Sheet status update error: {e}")

async def send_notifications(result, user_data, profile, photo_file_id=None, feedback_id=None, context=None):
    try:
        from telegram import Bot
        bot = Bot(token=BOT_TOKEN)
        category = result.get("category", "—")
        icon = CATEGORY_ICONS.get(category, "📌")
        sender = user_data.get("sender_name", "Аноним")
        now = datetime.now()

        safe_id = (feedback_id or "").replace("-", "_")
        id_str = f"`{feedback_id}`" if feedback_id else ""

        text = (
            f"📌 {id_str} · {icon} *{category}*\n"
            f"{result.get('summary', '—')}\n"
            f"_{now.strftime('%d.%m %H:%M')} · {sender}_"
        )

        keyboard = build_task_keyboard(safe_id)
        recipients = get_recipients(category)
        msg_ids = {}

        logger.info(f"Sending notifications: category={category}, recipients={len(recipients)}, photo={'yes' if photo_file_id else 'no'}")

        for uid in recipients:
            # Текстове повідомлення
            try:
                msg = await bot.send_message(chat_id=uid, text=text, parse_mode="Markdown", reply_markup=keyboard)
                msg_ids[str(uid)] = msg.message_id
                track_msg(uid, msg.message_id)
                if context:
                    schedule_delete(context, bot, uid, msg.message_id, delay=TASK_DELETE_DELAY)
                logger.info(f"Text notification sent to {uid}")
            except Exception as e:
                logger.error(f"Notify text error {uid}: {e}")
                continue

            # Фото — окремий try/except щоб не блокувати решту
            if photo_file_id:
                try:
                    photo_msg = await bot.send_photo(chat_id=uid, photo=photo_file_id)
                    track_msg(uid, photo_msg.message_id)
                    if context:
                        schedule_delete(context, bot, uid, photo_msg.message_id, delay=TASK_DELETE_DELAY)
                    logger.info(f"Photo sent to {uid}, file_id={photo_file_id}")
                except Exception as e:
                    logger.error(f"Photo send error to {uid}: {e}")

        if feedback_id and msg_ids:
            msg_store[feedback_id] = {"ids": msg_ids, "text": text}
            save_msg_store(msg_store)

    except Exception as e:
        logger.error(f"Notification error: {e}")


# ─── СТАТУСИ ЗАДАЧ ────────────────────────────────────────────────────────────

async def handle_status_update(update, context):
    """Обработчик кнопок статуса на уведомлениях."""
    query = update.callback_query
    await safe_answer(query)

    parts = query.data.split("_", 2)
    if len(parts) < 3:
        await safe_answer(query, "Невірний формат.", show_alert=True)
        return

    action = parts[1]
    safe_id = parts[2]
    feedback_id = safe_id.replace("_", "-", 2)

    user_id = update.effective_user.id
    assigned_info = assign_store.get(feedback_id, {})
    is_assignee = assigned_info.get("assignee_id") == user_id

    if not can_manage_tasks(user_id) and not is_assignee:
        await safe_answer(query, "Немає прав.", show_alert=True)
        return

    profile = user_profiles.get(user_id, {})
    who = get_display_name(profile)
    now = datetime.now().strftime("%d.%m %H:%M")
    assigned = feedback_id in assign_store

    # ── Видалення задачі (тільки власник) ────────────────────────────────────
    if action == "del":
        if not is_owner(user_id):
            await safe_answer(query, "Тільки власник може видаляти.", show_alert=True)
            return
        from telegram import Bot
        bot = Bot(token=BOT_TOKEN)
        msg_ids, _ = get_msg_data(feedback_id)
        for uid_str, mid in msg_ids.items():
            ids = mid if isinstance(mid, list) else [mid]
            for m in ids:
                try:
                    await bot.delete_message(chat_id=int(uid_str), message_id=m)
                except Exception:
                    pass
        if feedback_id in msg_store:
            del msg_store[feedback_id]
            save_msg_store(msg_store)
        asyncio.create_task(update_sheet_status(feedback_id, "Видалено", who))
        asyncio.create_task(update_sheet_history(feedback_id, f"🗑️ Видалено — {who} {now}"))
        return
    # ─────────────────────────────────────────────────────────────────────────

    if action == "done":
        status_text = f"✅ Виконано — {who} {now}"
        new_keyboard = build_task_keyboard(safe_id, assigned=assigned, done=True)
        sheet_status = f"Виконано — {who}"
        history_entry = f"✅ Виконано — {who} {now}"
    elif action == "wip":
        status_text = f"🔄 В роботі — {who} {now}"
        new_keyboard = build_wip_keyboard(safe_id, assigned=assigned)
        sheet_status = f"В роботі — {who}"
        history_entry = f"🔄 В роботі — {who} {now}"
    else:  # cancel
        status_text = f"↩️ Скасовано — {who} {now}"
        new_keyboard = build_task_keyboard(safe_id, assigned=assigned)
        sheet_status = "Нове"
        history_entry = f"↩️ Скасовано — {who} {now}"

    _, stored_text = get_msg_data(feedback_id)
    base_text = stored_text if stored_text else query.message.text
    new_text = base_text + f"\n{status_text}"

    from telegram import Bot
    bot = Bot(token=BOT_TOKEN)
    # Спочатку редагуємо повідомлення — миттєво для користувача
    await edit_all_messages(bot, feedback_id, new_text, new_keyboard)
    # Потім пишемо в таблицю фоном
    asyncio.create_task(update_sheet_status(feedback_id, sheet_status, ""))
    asyncio.create_task(update_sheet_history(feedback_id, history_entry))


# ─── ДОРУЧЕННЯ (ASSIGN) ───────────────────────────────────────────────────────

async def handle_assign(update, context):
    """Показує список співробітників для доручення."""
    query = update.callback_query
    await safe_answer(query)

    if not can_manage_tasks(update.effective_user.id):
        await safe_answer(query, "Немає прав.", show_alert=True)
        return

    safe_id = query.data.replace("assign_", "")
    feedback_id = safe_id.replace("_", "-", 2)

    active = [(uid, p) for uid, p in user_profiles.items() if p.get("status") == STATUS_ACTIVE]
    if not active:
        await safe_answer(query, "Немає активних співробітників.", show_alert=True)
        return

    keyboard = [[InlineKeyboardButton(
        f"{get_display_name(p)} ({p.get('role', '—')})",
        callback_data=f"assignto_{safe_id}_{uid}"
    )] for uid, p in active]
    keyboard.append([InlineKeyboardButton("❌ Скасувати", callback_data=f"assigncancel_{safe_id}")])

    current = assign_store.get(feedback_id)
    title = f"🔁 Передоручити `{feedback_id}`:" if current else f"👤 Доручити `{feedback_id}`:"
    await query.message.reply_text(title, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def handle_assign_cancel(update, context):
    query = update.callback_query
    await safe_answer(query)
    await query.edit_message_text("❌ Скасовано.")
    from telegram import Bot
    bot = Bot(token=BOT_TOKEN)
    # Видаляємо через 30 сек
    schedule_delete(context, bot, query.message.chat_id, query.message.message_id, delay=30)

async def handle_assign_to(update, context):
    """Призначає задачу конкретному співробітнику."""
    query = update.callback_query
    await safe_answer(query)

    if not can_manage_tasks(update.effective_user.id):
        await safe_answer(query, "Немає прав.", show_alert=True)
        return

    data = query.data.replace("assignto_", "")
    parts = data.rsplit("_", 1)
    safe_id = parts[0]
    target_uid = int(parts[1])
    feedback_id = safe_id.replace("_", "-", 2)

    assigner = user_profiles.get(update.effective_user.id, {})
    assigner_name = get_display_name(assigner)
    target_profile = user_profiles.get(target_uid, {})
    target_name = get_display_name(target_profile)
    now = datetime.now().strftime("%d.%m %H:%M")

    old = assign_store.get(feedback_id)
    if old:
        from telegram import Bot
        bot_notify = Bot(token=BOT_TOKEN)
        try:
            await bot_notify.send_message(
                chat_id=old["assignee_id"],
                text=f"❌ Завдання `{feedback_id}` передоручено іншому співробітнику.",
                parse_mode="Markdown"
            )
        except Exception:
            pass

    assign_store[feedback_id] = {
        "assignee_id": target_uid,
        "assignee_name": target_name,
        "assigned_by": assigner_name,
        "assigned_at": now,
    }
    save_assign_store(assign_store)

    assign_line = f"\n👤 Доручено: {target_name} — {assigner_name} {now}"
    _, stored_text = get_msg_data(feedback_id)
    base_text = stored_text if stored_text else query.message.text
    lines = [l for l in base_text.split("\n") if not l.startswith("👤 Доручено:")]
    new_text = "\n".join(lines) + assign_line
    new_keyboard = build_task_keyboard(safe_id, assigned=True)

    from telegram import Bot
    bot = Bot(token=BOT_TOKEN)
    await edit_all_messages(bot, feedback_id, new_text, new_keyboard)

    # Беремо повний текст із msg_store (з коментарями), фільтруємо лише статуси і доручення
    _, stored = get_msg_data(feedback_id)
    base = stored if stored else query.message.text
    task_lines = [l for l in base.split("\n") if not l.startswith(("✅", "🔄", "↩️", "👤 Доручено:"))]
    task_text = "\n".join(task_lines).strip()

    assignee_keyboard = build_task_keyboard(safe_id)
    try:
        msg = await bot.send_message(
            chat_id=target_uid,
            text=f"📋 *Нове завдання — `{feedback_id}`*\n{task_text}\n\n_Доручив: {assigner_name} {now}_",
            parse_mode="Markdown",
            reply_markup=assignee_keyboard
        )
        if feedback_id in msg_store and isinstance(msg_store[feedback_id], dict):
            existing = msg_store[feedback_id]["ids"].get(str(target_uid))
            if existing is None:
                msg_store[feedback_id]["ids"][str(target_uid)] = msg.message_id
            elif isinstance(existing, list):
                existing.append(msg.message_id)
            else:
                msg_store[feedback_id]["ids"][str(target_uid)] = [existing, msg.message_id]
        else:
            msg_store[feedback_id] = {"ids": {str(target_uid): msg.message_id}, "text": task_text}
        save_msg_store(msg_store)
    except Exception as e:
        logger.error(f"Assign send error: {e}")

    await update_sheet_history(feedback_id, f"👤 Доручено {target_name} — {assigner_name} {now}")
    await query.edit_message_text(f"✅ Завдання `{feedback_id}` доручено *{target_name}*", parse_mode="Markdown")
    # Видаляємо підтвердження через 60 сек
    schedule_delete(context, bot, query.message.chat_id, query.message.message_id, delay=60)


# ─── КОМЕНТАРІ ────────────────────────────────────────────────────────────────

async def handle_comment(update, context):
    """Входим в режим комментария."""
    query = update.callback_query
    await safe_answer(query)

    safe_id = query.data.replace("comment_", "")
    feedback_id = safe_id.replace("_", "-", 2)
    user_id = update.effective_user.id

    cancel_user_jobs(context, user_id, "commentclear")

    context.user_data['commenting_on'] = feedback_id
    context.user_data['comment_safe_id'] = safe_id

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Скасувати", callback_data="comment_cancel_mode")
    ]])
    sent = await query.message.reply_text(
        f"💬 Напишіть коментар до `{feedback_id}`:\n_Автоматично скасується через 2 хвилини._",
        parse_mode="Markdown",
        reply_markup=keyboard
    )
    # Зберігаємо msg_id промпту щоб потім редагувати замість нового повідомлення
    context.user_data['comment_prompt_msg_id'] = sent.message_id
    context.user_data['comment_prompt_chat_id'] = sent.chat_id

    context.job_queue.run_once(
        comment_clear_callback,
        when=120,
        name=f"commentclear_{user_id}",
        data={"user_id": user_id, "chat_id": update.effective_chat.id}
    )

async def comment_clear_callback(context):
    """Сбрасывает режим комментария через 2 минуты."""
    user_id = context.job.data["user_id"]
    chat_id = context.job.data["chat_id"]
    user_data = context.application.user_data.get(user_id, {})
    if user_data.get('commenting_on'):
        user_data.pop('commenting_on', None)
        user_data.pop('comment_safe_id', None)
        prompt_msg_id = user_data.pop('comment_prompt_msg_id', None)
        prompt_chat_id = user_data.pop('comment_prompt_chat_id', None)
        from telegram import Bot
        bot = Bot(token=BOT_TOKEN)
        if prompt_msg_id and prompt_chat_id:
            try:
                await bot.edit_message_text(
                    chat_id=prompt_chat_id,
                    message_id=prompt_msg_id,
                    text="⏱ Час вийшов. Режим коментаря скасовано."
                )
                # Видаляємо через 30 сек
                schedule_delete(context, bot, prompt_chat_id, prompt_msg_id, delay=30)
            except Exception:
                pass
        else:
            try:
                sent = await bot.send_message(chat_id=chat_id, text="⏱ Час вийшов. Режим коментаря скасовано.")
                schedule_delete(context, bot, chat_id, sent.message_id, delay=30)
            except Exception:
                pass

async def handle_comment_cancel_mode(update, context):
    """Отмена режима комментария."""
    query = update.callback_query
    await safe_answer(query)
    cancel_user_jobs(context, update.effective_user.id, "commentclear")
    context.user_data.pop('commenting_on', None)
    context.user_data.pop('comment_safe_id', None)
    context.user_data.pop('comment_prompt_msg_id', None)
    context.user_data.pop('comment_prompt_chat_id', None)
    await query.edit_message_text("❌ Коментар скасовано.")
    from telegram import Bot
    bot = Bot(token=BOT_TOKEN)
    # Видаляємо через 30 сек
    schedule_delete(context, bot, query.message.chat_id, query.message.message_id, delay=30)

async def process_comment(update, context):
    """Дописує коментар в оригінальне повідомлення."""
    user_id = update.effective_user.id
    feedback_id = context.user_data.pop('commenting_on', None)
    safe_id = context.user_data.pop('comment_safe_id', None)
    prompt_msg_id = context.user_data.pop('comment_prompt_msg_id', None)
    prompt_chat_id = context.user_data.pop('comment_prompt_chat_id', None)

    cancel_user_jobs(context, user_id, "commentclear")

    if not feedback_id:
        return False

    profile = user_profiles.get(user_id, {})
    who = get_display_name(profile)
    now = datetime.now().strftime("%d.%m %H:%M")
    comment_text = update.message.text.strip()
    comment_line = f"💬 {who} {now}: {comment_text}"

    _, stored_text = get_msg_data(feedback_id)
    new_text = (stored_text + f"\n{comment_line}") if stored_text else comment_line

    assigned = feedback_id in assign_store
    keyboard = build_task_keyboard(safe_id or "", assigned=assigned)

    from telegram import Bot
    bot = Bot(token=BOT_TOKEN)
    await edit_all_messages(bot, feedback_id, new_text, keyboard)
    await update_sheet_history(feedback_id, comment_line)

    # Редагуємо промпт замість нового повідомлення
    if prompt_msg_id and prompt_chat_id:
        try:
            await bot.edit_message_text(
                chat_id=prompt_chat_id,
                message_id=prompt_msg_id,
                text=f"✅ Коментар до `{feedback_id}` збережено.",
                parse_mode="Markdown"
            )
            # Видаляємо підтвердження через 60 сек
            schedule_delete(context, bot, prompt_chat_id, prompt_msg_id, delay=60)
        except Exception:
            sent = await update.message.reply_text(
                f"✅ Коментар до `{feedback_id}` збережено.", parse_mode="Markdown"
            )
            schedule_delete(context, bot, sent.chat_id, sent.message_id, delay=60)
    else:
        sent = await update.message.reply_text(
            f"✅ Коментар до `{feedback_id}` збережено.", parse_mode="Markdown"
        )
        schedule_delete(context, bot, sent.chat_id, sent.message_id, delay=60)

    return True


# ─── ДАЙДЖЕСТ / ДНИ НАРОДЖЕННЯ ───────────────────────────────────────────────

def build_row_text(row, fid, safe_id):
    """Формує текст і клавіатуру для рядка задачі з таблиці."""
    status = row.get("Статус", "Нове")
    status_icon = "🔴" if status == "Нове" else "🟡"
    category = row.get("Категорія", "—")
    cat_icon = CATEGORY_ICONS.get(category, "📌")
    date = row.get("Дата", "—")
    time_val = row.get("Час", "")
    sender = row.get("Хто повідомив", "—")
    summary = row.get("Суть", "—")
    history = row.get("Лог", "")
    date_str = f"{date} {time_val}".strip()

    assigned = fid in assign_store
    assign_info = assign_store.get(fid, {})

    text = (
        f"{status_icon} `{fid}` · {cat_icon} *{category}*\n"
        f"{summary}\n"
        f"_{date_str} · {sender}_"
    )
    if assign_info:
        text += f"\n👤 Доручено: {assign_info.get('assignee_name', '—')} — {assign_info.get('assigned_by', '—')} {assign_info.get('assigned_at', '')}"
    if history and history != "Нове":
        entries = [e.strip() for e in history.split("|") if e.strip() and e.strip() != "Нове"]
        if entries:
            text += "\n\n📋 _Історія:_\n" + "\n".join(f"_{e}_" for e in entries)

    keyboard = build_task_keyboard(safe_id, assigned=assigned)
    return text, keyboard

async def send_unresolved_digest(context):
    """Відправляє о 06:00 UTC (09:00 Київ) дайджест:
    1. Видаляє попередні відстежені повідомлення у всіх отримувачів
    2. Повідомлення 1: виконані вчора (без кнопок)
    3. Повідомлення 2+: кожна невиконана задача окремо з кнопками
    """
    try:
        from telegram import Bot
        bot = Bot(token=BOT_TOKEN)
        gc = get_sheets_client()
        sheet = gc.open_by_key(SPREADSHEET_ID).worksheet("Зворотний зв'язок")
        rows = sheet.get_all_records()

        recipients = list(get_recipients())

        yesterday = (datetime.now() - __import__('datetime').timedelta(days=1)).strftime("%d.%m.%Y")
        today_str = datetime.now().strftime("%d.%m.%Y")

        unresolved = [
            r for r in rows
            if "Виконано" not in str(r.get("Статус", ""))
            and "Видалено" not in str(r.get("Статус", ""))
        ]
        completed_yesterday = [
            r for r in rows
            if "Виконано" in str(r.get("Статус", ""))
            and r.get("Дата", "") == yesterday
        ]

        # ── 1. Очищення чату ──────────────────────────────────────────────────
        await delete_tracked_messages(bot, recipients)

        # ── 2. Виконані вчора ─────────────────────────────────────────────────
        if completed_yesterday:
            done_text = f"✅ *Виконано вчора ({yesterday}): {len(completed_yesterday)}*\n\n"
            for row in completed_yesterday:
                fid = row.get("Номер", "—")
                category = row.get("Категорія", "—")
                cat_icon = CATEGORY_ICONS.get(category, "📌")
                summary = row.get("Суть", "—")
                status = row.get("Статус", "—")
                done_text += f"✅ `{fid}` · {cat_icon} {summary}\n_{status}_\n\n"
            for uid in recipients:
                try:
                    msg = await bot.send_message(chat_id=uid, text=done_text.strip(), parse_mode="Markdown")
                    track_msg(uid, msg.message_id)
                except Exception as e:
                    logger.error(f"Digest done_yesterday error {uid}: {e}")
        else:
            for uid in recipients:
                try:
                    msg = await bot.send_message(
                        chat_id=uid,
                        text=f"✅ *Вчора ({yesterday}) виконаних завдань немає.*",
                        parse_mode="Markdown"
                    )
                    track_msg(uid, msg.message_id)
                except Exception as e:
                    logger.error(f"Digest no_done error {uid}: {e}")

        # ── 3. Невиконані завдання ────────────────────────────────────────────
        if not unresolved:
            for uid in recipients:
                try:
                    msg = await bot.send_message(chat_id=uid, text="🎉 *Всі завдання виконано!*", parse_mode="Markdown")
                    track_msg(uid, msg.message_id)
                except Exception:
                    pass
            return

        header = f"🔴 *Невиконані завдання: {len(unresolved)}*"
        for uid in recipients:
            try:
                msg = await bot.send_message(chat_id=uid, text=header, parse_mode="Markdown")
                track_msg(uid, msg.message_id)
            except Exception:
                pass

        for row in unresolved:
            fid = row.get("Номер", "—")
            safe_id = fid.replace("-", "_")
            _, keyboard = build_row_text(row, fid, safe_id)
            raw = msg_store.get(fid)
            stored_text = raw.get("text", "") if isinstance(raw, dict) else ""
            text = stored_text if stored_text else build_row_text(row, fid, safe_id)[0]
            for uid in recipients:
                try:
                    msg = await bot.send_message(chat_id=uid, text=text, parse_mode="Markdown", reply_markup=keyboard)
                    # Трекаємо задачне повідомлення + таймер 24 год
                    track_msg(uid, msg.message_id)
                    schedule_delete(context, bot, uid, msg.message_id, delay=TASK_DELETE_DELAY)
                    if fid != "—":
                        if fid not in msg_store or not isinstance(msg_store.get(fid), dict):
                            msg_store[fid] = {"ids": {}, "text": text}
                        existing = msg_store[fid]["ids"].get(str(uid))
                        if existing is None:
                            msg_store[fid]["ids"][str(uid)] = msg.message_id
                        elif isinstance(existing, list):
                            existing.append(msg.message_id)
                        else:
                            msg_store[fid]["ids"][str(uid)] = [existing, msg.message_id]
                except Exception as e:
                    logger.error(f"Digest item error {uid}: {e}")
            if fid != "—":
                save_msg_store(msg_store)

    except Exception as e:
        logger.error(f"Unresolved digest error: {e}")

async def send_birthday_greetings(context):
    try:
        from telegram import Bot
        bot = Bot(token=BOT_TOKEN)
        today = datetime.now().strftime("%d.%m")
        birthday_users = [(uid, p) for uid, p in user_profiles.items()
                          if p.get("status") == STATUS_ACTIVE and p.get("birthday", "")[:5] == today]
        for uid, profile in birthday_users:
            try:
                await bot.send_message(chat_id=uid, text=f"🎂 З Днем народження, {profile.get('first_name', '')}! 🎉\nВітаємо від усієї команди! Бажаємо здоров'я та успіхів! 🥳")
            except Exception as e:
                logger.error(f"Birthday error {uid}: {e}")
        if birthday_users:
            names = ", ".join(get_display_name(p) for _, p in birthday_users)
            for admin_id in get_recipients():
                try:
                    await bot.send_message(chat_id=admin_id, text=f"🎂 Сьогодні день народження у: *{names}*", parse_mode="Markdown")
                except Exception as e:
                    logger.error(f"Birthday admin error: {e}")
    except Exception as e:
        logger.error(f"Birthday greetings error: {e}")


async def prompt_edit_timeout_callback(context):
    user_id = context.job.data["user_id"]
    user_data = context.application.user_data.get(user_id, {})
    user_data.pop('prompt_editing', None)
    user_data.pop('prompt_original', None)
    user_data.pop('prompt_working', None)
    user_data.pop('prompt_msg_id', None)
    user_data.pop('prompt_chat_id', None)

def refine_prompt_with_claude(current_prompt, instruction):
    """Генерує новий варіант промпту на основі поточного і голосової інструкції."""
    meta_prompt = f"""Ти редагуєш системний промпт для Telegram-бота, який аналізує повідомлення персоналу ресторану.

Поточний промпт:
---
{current_prompt}
---

Інструкція власника (що треба змінити):
"{instruction}"

Поверни ТІЛЬКИ оновлений промпт без будь-яких пояснень. Обов'язково збережи плейсхолдери <<restaurant>>, <<role_line>>, <<message_text>> на своїх місцях."""
    response = claude_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1500,
        messages=[{"role": "user", "content": meta_prompt}]
    )
    return response.content[0].text.strip()

async def process_prompt_voice(update, context):
    """Обробляє голосове повідомлення власника в режимі редагування промпту."""
    user_id = update.effective_user.id
    from telegram import Bot
    bot = Bot(token=BOT_TOKEN)

    cancel_user_jobs(context, user_id, "promptedit")

    # Показуємо статус прямо в меню-повідомленні
    prompt_msg_id = context.user_data.get('prompt_msg_id')
    prompt_chat_id = context.user_data.get('prompt_chat_id')
    try:
        await bot.edit_message_text(
            chat_id=prompt_chat_id,
            message_id=prompt_msg_id,
            text="⏳ Розпізнаю голосове...",
            reply_markup=None
        )
    except Exception:
        pass

    # Транскрипція
    voice_file = await update.message.voice.get_file()
    file_path = os.path.join(tempfile.gettempdir(), f"promptvoice_{user_id}.ogg")
    await voice_file.download_to_drive(file_path)
    instruction = await transcribe_voice(file_path)

    if not instruction:
        working = context.user_data.get('prompt_working', load_prompt())
        display = working if len(working) <= 3000 else working[:3000] + "\n...(скорочено)"
        try:
            await bot.edit_message_text(
                chat_id=prompt_chat_id,
                message_id=prompt_msg_id,
                text=f"❌ Не вдалося розпізнати. Спробуйте ще раз.\n\n`{display}`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Скасувати", callback_data="menu_cancel_edit_prompt")]])
            )
        except Exception:
            pass
        context.job_queue.run_once(prompt_edit_timeout_callback, when=300,
            name=f"promptedit_{user_id}", data={"user_id": user_id})
        return

    # Генерація нового промпту
    try:
        await bot.edit_message_text(
            chat_id=prompt_chat_id, message_id=prompt_msg_id,
            text="⏳ Генерую новий промпт...", reply_markup=None
        )
    except Exception:
        pass

    try:
        current = context.user_data.get('prompt_working', load_prompt())
        new_prompt = refine_prompt_with_claude(current, instruction)
    except Exception as e:
        logger.error(f"Prompt refinement error: {e}")
        new_prompt = None

    if not new_prompt:
        try:
            await bot.edit_message_text(
                chat_id=prompt_chat_id, message_id=prompt_msg_id,
                text="❌ Помилка генерації. Спробуйте ще раз.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Скасувати", callback_data="menu_cancel_edit_prompt")]])
            )
        except Exception:
            pass
        context.job_queue.run_once(prompt_edit_timeout_callback, when=300,
            name=f"promptedit_{user_id}", data={"user_id": user_id})
        return

    context.user_data['prompt_working'] = new_prompt

    display = new_prompt if len(new_prompt) <= 3000 else new_prompt[:3000] + "\n...(скорочено)"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Прийняти", callback_data="menu_accept_prompt")],
        [InlineKeyboardButton("🎙 Редагувати далі", callback_data="menu_edit_prompt")],
        [InlineKeyboardButton("❌ Скасувати", callback_data="menu_cancel_edit_prompt")],
    ])
    try:
        await bot.edit_message_text(
            chat_id=prompt_chat_id,
            message_id=prompt_msg_id,
            text=f"📋 *Новий варіант промпту:*\n\n`{display}`",
            parse_mode="Markdown",
            reply_markup=keyboard
        )
    except Exception as e:
        logger.error(f"Edit prompt msg error: {e}")

# ─── АДМІН-МЕНЮ ──────────────────────────────────────────────────────────────

async def show_admin_menu(update, context):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("❌ Немає прав.")
        return
    keyboard = [
        [InlineKeyboardButton("🔴 Невиконані завдання", callback_data="menu_unresolved")],
        [InlineKeyboardButton("👥 Список співробітників", callback_data="menu_staff")],
        [InlineKeyboardButton("⏳ Очікують підтвердження", callback_data="menu_pending")],
        [InlineKeyboardButton("🔄 Змінити роль", callback_data="menu_changerole")],
        [InlineKeyboardButton("❌ Звільнити", callback_data="menu_fire")],
        [InlineKeyboardButton("♻️ Відновити співробітника", callback_data="menu_restore")],
    ]
    if is_owner(user_id):
        keyboard.append([InlineKeyboardButton("📋 Поточний промпт", callback_data="menu_view_prompt")])
        keyboard.append([InlineKeyboardButton("✏️ Змінити промпт", callback_data="menu_edit_prompt")])
    msg = await update.message.reply_text(
        "👨‍💼 *Адмін-меню*\nОберіть дію:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    track_msg(msg.chat_id, msg.message_id)
    from telegram import Bot as _Bot
    _bot = _Bot(token=BOT_TOKEN)
    schedule_delete(context, _bot, msg.chat_id, msg.message_id, delay=MENU_DELETE_DELAY)

async def handle_admin_menu(update, context):
    query = update.callback_query
    await safe_answer(query)
    user_id = update.effective_user.id

    if not is_admin(user_id):
        await safe_answer(query, "Немає прав.", show_alert=True)
        return

    action = query.data.replace("menu_", "")

    if action == "unresolved":
        await show_unresolved(query, context)
        return

    elif action == "staff":
        active = [(uid, p) for uid, p in user_profiles.items() if p.get("status") == STATUS_ACTIVE]
        text = f"👥 *Активні співробітники: {len(active)}*\n\n"
        for uid, p in sorted(active, key=lambda x: x[1].get("role", "")):
            text += f"• {get_display_name(p)} — {p.get('role', '—')} ({p.get('restaurant', '—')})\n"
        await query.edit_message_text(text, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="menu_back")]]))

    elif action == "pending":
        pending = [(uid, p) for uid, p in user_profiles.items() if p.get("status") == STATUS_PENDING]
        if not pending:
            await query.edit_message_text("✅ Немає заявок.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="menu_back")]]))
            return
        for uid, profile in pending:
            full_name = get_display_name(profile)
            text = (f"⏳ *{full_name}*\n💼 {profile.get('role', '—')}\n"
                    f"🏠 {profile.get('restaurant', '—')}\n📱 {profile.get('phone', '—')}")
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅", callback_data=f"approve_{uid}"),
                InlineKeyboardButton("❌", callback_data=f"reject_{uid}"),
            ]])
            await query.message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")
        await query.edit_message_text("👆 Заявки вище:", parse_mode="Markdown")

    elif action == "changerole":
        active = [(uid, p) for uid, p in user_profiles.items() if p.get("status") == STATUS_ACTIVE]
        if not active:
            await query.edit_message_text("Немає активних співробітників.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="menu_back")]]))
            return
        keyboard = [[InlineKeyboardButton(
            f"{get_display_name(p)} ({p.get('role', '—')})",
            callback_data=f"adminrole_{uid}"
        )] for uid, p in active]
        keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="menu_back")])
        await query.edit_message_text("Оберіть співробітника:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif action == "fire":
        active = [(uid, p) for uid, p in user_profiles.items() if p.get("status") == STATUS_ACTIVE]
        if not active:
            await query.edit_message_text("Немає активних співробітників.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="menu_back")]]))
            return
        keyboard = [[InlineKeyboardButton(
            f"{get_display_name(p)} ({p.get('role', '—')})",
            callback_data=f"fire_{uid}"
        )] for uid, p in active]
        keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="menu_back")])
        await query.edit_message_text("Оберіть співробітника для звільнення:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif action == "restore":
        fired = [(uid, p) for uid, p in user_profiles.items() if p.get("status") == STATUS_FIRED]
        if not fired:
            await query.edit_message_text("✅ Немає звільнених співробітників.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="menu_back")]]))
            return
        keyboard = [[InlineKeyboardButton(
            f"{get_display_name(p)} ({p.get('role', '—')})",
            callback_data=f"restore_{uid}"
        )] for uid, p in fired]
        keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="menu_back")])
        await query.edit_message_text("Оберіть співробітника для відновлення:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif action == "back":
        keyboard = [
            [InlineKeyboardButton("🔴 Невиконані завдання", callback_data="menu_unresolved")],
            [InlineKeyboardButton("👥 Список співробітників", callback_data="menu_staff")],
            [InlineKeyboardButton("⏳ Очікують підтвердження", callback_data="menu_pending")],
            [InlineKeyboardButton("🔄 Змінити роль", callback_data="menu_changerole")],
            [InlineKeyboardButton("❌ Звільнити", callback_data="menu_fire")],
            [InlineKeyboardButton("♻️ Відновити співробітника", callback_data="menu_restore")],
        ]
        if is_owner(user_id):
            keyboard.append([InlineKeyboardButton("📋 Поточний промпт", callback_data="menu_view_prompt")])
            keyboard.append([InlineKeyboardButton("✏️ Змінити промпт", callback_data="menu_edit_prompt")])
        await query.edit_message_text("👨‍💼 *Адмін-меню*\nОберіть дію:",
            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

    elif action == "view_prompt":
        if not is_owner(user_id):
            await safe_answer(query, "Тільки для власника.", show_alert=True)
            return
        template = load_prompt()
        display = template if len(template) <= 3800 else template[:3800] + "\n...(скорочено)"
        await query.edit_message_text(
            f"📋 *Поточний промпт:*\n\n`{display}`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✏️ Змінити промпт", callback_data="menu_edit_prompt")],
                [InlineKeyboardButton("◀️ Назад", callback_data="menu_back")],
            ])
        )

    elif action == "edit_prompt":
        if not is_owner(user_id):
            await safe_answer(query, "Тільки для власника.", show_alert=True)
            return
        # Ініціалізуємо стан тільки при першому вході
        if not context.user_data.get('prompt_original'):
            current = load_prompt()
            context.user_data['prompt_original'] = current
            context.user_data['prompt_working'] = current
        context.user_data['prompt_editing'] = True
        context.user_data['prompt_msg_id'] = query.message.message_id
        context.user_data['prompt_chat_id'] = query.message.chat_id
        working = context.user_data['prompt_working']
        display = working if len(working) <= 3000 else working[:3000] + "\n...(скорочено)"
        await query.edit_message_text(
            f"✏️ *Режим редагування промпту*\n\n`{display}`\n\n"
            "🎙 Надішліть голосове повідомлення з інструкцією що змінити.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Скасувати", callback_data="menu_cancel_edit_prompt")],
            ])
        )
        cancel_user_jobs(context, user_id, "promptedit")
        context.job_queue.run_once(
            prompt_edit_timeout_callback,
            when=300,
            name=f"promptedit_{user_id}",
            data={"user_id": user_id}
        )

    elif action == "accept_prompt":
        if not is_owner(user_id):
            await safe_answer(query, "Тільки для власника.", show_alert=True)
            return
        cancel_user_jobs(context, user_id, "promptedit")
        new_prompt = context.user_data.pop('prompt_working', None)
        context.user_data.pop('prompt_original', None)
        context.user_data.pop('prompt_editing', None)
        context.user_data.pop('prompt_msg_id', None)
        context.user_data.pop('prompt_chat_id', None)
        if new_prompt:
            save_prompt(new_prompt)
        await query.edit_message_text(
            "✅ *Промпт збережено!*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ До меню", callback_data="menu_back")]])
        )

    elif action == "cancel_edit_prompt":
        cancel_user_jobs(context, user_id, "promptedit")
        context.user_data.pop('prompt_editing', None)
        context.user_data.pop('prompt_working', None)
        context.user_data.pop('prompt_original', None)
        context.user_data.pop('prompt_msg_id', None)
        context.user_data.pop('prompt_chat_id', None)
        keyboard = [
            [InlineKeyboardButton("🔴 Невиконані завдання", callback_data="menu_unresolved")],
            [InlineKeyboardButton("👥 Список співробітників", callback_data="menu_staff")],
            [InlineKeyboardButton("⏳ Очікують підтвердження", callback_data="menu_pending")],
            [InlineKeyboardButton("🔄 Змінити роль", callback_data="menu_changerole")],
            [InlineKeyboardButton("❌ Звільнити", callback_data="menu_fire")],
            [InlineKeyboardButton("♻️ Відновити співробітника", callback_data="menu_restore")],
            [InlineKeyboardButton("📋 Поточний промпт", callback_data="menu_view_prompt")],
            [InlineKeyboardButton("✏️ Змінити промпт", callback_data="menu_edit_prompt")],
        ]
        await query.edit_message_text("❌ Редагування скасовано.\n\n👨‍💼 *Адмін-меню*\nОберіть дію:",
            reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def show_unresolved(query, context):
    """Показує невиконані завдання з повною клавіатурою і історією."""
    try:
        gc = get_sheets_client()
        sheet = gc.open_by_key(SPREADSHEET_ID).worksheet("Зворотний зв'язок")
        rows = sheet.get_all_records()
        unresolved = [
            r for r in rows
            if "Виконано" not in str(r.get("Статус", ""))
            and "Видалено" not in str(r.get("Статус", ""))
        ]

        if not unresolved:
            await query.edit_message_text(
                "✅ *Всі завдання виконано!*",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="menu_back")]]))
            return

        await query.edit_message_text(
            f"🔴 *Невиконані завдання: {len(unresolved)}*",
            parse_mode="Markdown"
        )
        uid = query.from_user.id
        for row in unresolved:
            fid = row.get("Номер", "—")
            safe_id = fid.replace("-", "_")
            _, keyboard = build_row_text(row, fid, safe_id)
            raw = msg_store.get(fid)
            stored_text = raw.get("text", "") if isinstance(raw, dict) else ""
            text = stored_text if stored_text else build_row_text(row, fid, safe_id)[0]
            try:
                msg = await query.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)
                if fid != "—":
                    if fid not in msg_store or not isinstance(msg_store.get(fid), dict):
                        msg_store[fid] = {"ids": {}, "text": text}
                    existing = msg_store[fid]["ids"].get(str(uid))
                    if existing is None:
                        msg_store[fid]["ids"][str(uid)] = msg.message_id
                    elif isinstance(existing, list):
                        existing.append(msg.message_id)
                    else:
                        msg_store[fid]["ids"][str(uid)] = [existing, msg.message_id]
                    save_msg_store(msg_store)
            except Exception as e:
                logger.error(f"Unresolved item send error: {e}")

    except Exception as e:
        logger.error(f"Show unresolved error: {e}")
        await query.edit_message_text("❌ Помилка при завантаженні завдань.")


# ─── УПРАВЛІННЯ ПЕРСОНАЛОМ ────────────────────────────────────────────────────

async def handle_restore(update, context):
    query = update.callback_query
    await safe_answer(query)
    if not is_admin(update.effective_user.id) and not is_owner(update.effective_user.id):
        await safe_answer(query, "Немає прав.", show_alert=True)
        return
    user_id = int(query.data.replace("restore_", ""))
    if user_id in user_profiles:
        user_profiles[user_id]["status"] = STATUS_ACTIVE
        save_profiles(user_profiles)
        full_name = get_display_name(user_profiles[user_id])
        await query.edit_message_text(f"✅ {full_name} — відновлено!")
        from telegram import Bot
        bot = Bot(token=BOT_TOKEN)
        try:
            await bot.send_message(chat_id=user_id, text="✅ Ваш доступ до бота відновлено! Ласкаво просимо назад 🎉", reply_markup=get_main_keyboard(user_id))
        except Exception as e:
            logger.error(f"Restore notify error: {e}")

async def handle_admin_role_select(update, context):
    query = update.callback_query
    await safe_answer(query)
    target_uid = int(query.data.replace("adminrole_", ""))
    keyboard = [[InlineKeyboardButton(role, callback_data=f"adminsetrole_{target_uid}_{role}")] for role in ROLES]
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="menu_changerole")])
    profile = user_profiles.get(target_uid, {})
    await query.edit_message_text(
        f"Змінити роль для *{get_display_name(profile)}*\nПоточна: {profile.get('role', '—')}",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def handle_admin_set_role(update, context):
    query = update.callback_query
    await safe_answer(query)
    parts = query.data.replace("adminsetrole_", "").split("_", 1)
    target_uid = int(parts[0])
    new_role = parts[1]
    if target_uid in user_profiles:
        user_profiles[target_uid]["role"] = new_role
        save_profiles(user_profiles)
        full_name = get_display_name(user_profiles[target_uid])
        await query.edit_message_text(f"✅ Роль *{full_name}* змінено на *{new_role}*", parse_mode="Markdown")
        from telegram import Bot
        bot = Bot(token=BOT_TOKEN)
        try:
            await bot.send_message(chat_id=target_uid, text=f"✅ Вашу роль змінено на *{new_role}*", parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Role notify error: {e}")

async def handle_fire(update, context):
    query = update.callback_query
    await safe_answer(query)
    if not is_admin(update.effective_user.id) and not is_owner(update.effective_user.id):
        await safe_answer(query, "Немає прав.", show_alert=True)
        return
    user_id = int(query.data.replace("fire_", ""))
    if user_id in user_profiles:
        user_profiles[user_id]["status"] = STATUS_FIRED
        save_profiles(user_profiles)
        full_name = get_display_name(user_profiles[user_id])
        await query.edit_message_text(f"✅ {full_name} — звільнено.")
        from telegram import Bot
        bot = Bot(token=BOT_TOKEN)
        try:
            await bot.send_message(chat_id=user_id, text="❌ Ваш доступ до бота відхилено керівником.")
        except Exception as e:
            logger.error(f"Fire error: {e}")

async def show_profile(update, context):
    user_id = update.effective_user.id
    if user_id not in user_profiles:
        await update.message.reply_text("Ви ще не зареєстровані. /start")
        return
    profile = user_profiles[user_id]
    status_text = {"active": "✅ Активний", "pending": "⏳ Очікує підтвердження", "fired": "❌ Звільнений"}.get(profile.get("status"), "—")
    text = (
        f"👤 *Ваш профіль*\n\n"
        f"Ім'я: {get_display_name(profile)}\n"
        f"💼 Роль: {profile.get('role', '—')}\n"
        f"🏠 Заклад: {profile.get('restaurant', '—')}\n"
        f"📱 Телефон: {profile.get('phone', '—')}\n"
        f"🎂 День народження: {profile.get('birthday', '—')}\n"
        f"Статус: {status_text}"
    )
    keyboard = []
    if profile.get("status") == STATUS_ACTIVE:
        keyboard.append([InlineKeyboardButton("🔄 Заявка на зміну ролі", callback_data="request_role_change")])
    await update.message.reply_text(text, parse_mode="Markdown",
                                     reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None)

async def request_role_change(update, context):
    query = update.callback_query
    await safe_answer(query)
    keyboard = [[InlineKeyboardButton(role, callback_data=f"newrole_{role}")] for role in ROLES]
    await query.edit_message_text("Оберіть нову роль:", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_new_role(update, context):
    query = update.callback_query
    await safe_answer(query)
    user_id = update.effective_user.id
    new_role = query.data.replace("newrole_", "")
    profile = user_profiles.get(user_id, {})
    full_name = get_display_name(profile)
    from telegram import Bot
    bot = Bot(token=BOT_TOKEN)
    text = (f"🔄 *Заявка на зміну ролі*\n\n👤 {full_name}\n"
            f"📌 Поточна: {profile.get('role', '—')}\n➡️ Нова: {new_role}\n🏠 {profile.get('restaurant', '—')}")
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Підтвердити", callback_data=f"confirmrole_{user_id}_{new_role}"),
        InlineKeyboardButton("❌ Відхилити", callback_data=f"rejectrole_{user_id}"),
    ]])
    for admin_id in get_recipients():
        try:
            await bot.send_message(chat_id=admin_id, text=text, reply_markup=keyboard, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Role change notify error: {e}")
    await query.edit_message_text(f"✅ Заявку на зміну ролі на *{new_role}* подано! Очікуйте підтвердження.", parse_mode="Markdown")

async def handle_confirm_role(update, context):
    query = update.callback_query
    await safe_answer(query)
    if not is_admin(update.effective_user.id):
        await safe_answer(query, "Немає прав.", show_alert=True)
        return
    parts = query.data.replace("confirmrole_", "").split("_", 1)
    user_id = int(parts[0])
    new_role = parts[1]
    if user_id in user_profiles:
        user_profiles[user_id]["role"] = new_role
        save_profiles(user_profiles)
        full_name = get_display_name(user_profiles[user_id])
        await query.edit_message_text(f"✅ Роль {full_name} змінено на *{new_role}*", parse_mode="Markdown")
        from telegram import Bot
        bot = Bot(token=BOT_TOKEN)
        try:
            await bot.send_message(chat_id=user_id, text=f"✅ Вашу роль змінено на *{new_role}*", parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Confirm role error: {e}")

async def handle_reject_role(update, context):
    query = update.callback_query
    await safe_answer(query)
    if not is_admin(update.effective_user.id):
        await safe_answer(query, "Немає прав.", show_alert=True)
        return
    user_id = int(query.data.replace("rejectrole_", ""))
    if user_id in user_profiles:
        full_name = get_display_name(user_profiles[user_id])
        await query.edit_message_text(f"❌ Заявку {full_name} відхилено.")
        from telegram import Bot
        bot = Bot(token=BOT_TOKEN)
        try:
            await bot.send_message(chat_id=user_id, text="❌ Вашу заявку на зміну ролі відхилено.")
        except Exception as e:
            logger.error(f"Reject role error: {e}")

async def cmd_staff(update, context):
    user_id = update.effective_user.id
    if not is_admin(user_id) and not is_owner(user_id):
        await update.message.reply_text("❌ Немає прав.")
        return
    active = [(uid, p) for uid, p in user_profiles.items() if p.get("status") == STATUS_ACTIVE]
    pending = [(uid, p) for uid, p in user_profiles.items() if p.get("status") == STATUS_PENDING]
    text = f"👥 *Список співробітників*\n\n✅ Активних: {len(active)}\n⏳ Очікують: {len(pending)}\n\n"
    if active:
        text += "*Активні:*\n"
        for uid, p in active:
            text += f"• {get_display_name(p)} — {p.get('role', '—')} ({p.get('restaurant', '—')})\n"
    if pending:
        text += "\n*⏳ Очікують підтвердження:*\n"
        for uid, p in pending:
            text += f"• {get_display_name(p)} — {p.get('role', '—')}\n"
    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_confirm(update, context):
    user_id = update.effective_user.id
    if not is_admin(user_id) and not is_owner(user_id):
        await update.message.reply_text("❌ Немає прав.")
        return
    pending = [(uid, p) for uid, p in user_profiles.items() if p.get("status") == STATUS_PENDING]
    if not pending:
        await update.message.reply_text("✅ Немає заявок.")
        return
    for uid, profile in pending:
        full_name = get_display_name(profile)
        text = (f"⏳ *Заявка*\n\n👤 {full_name}\n💼 {profile.get('role', '—')}\n"
                f"🏠 {profile.get('restaurant', '—')}\n📱 {profile.get('phone', '—')}\n🎂 {profile.get('birthday', '—')}")
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Підтвердити", callback_data=f"approve_{uid}"),
            InlineKeyboardButton("❌ Відхилити", callback_data=f"reject_{uid}"),
        ]])
        await update.message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")

async def cmd_fire(update, context):
    user_id = update.effective_user.id
    if not is_admin(user_id) and not is_owner(user_id):
        await update.message.reply_text("❌ Немає прав.")
        return
    active = [(uid, p) for uid, p in user_profiles.items() if p.get("status") == STATUS_ACTIVE]
    if not active:
        await update.message.reply_text("Немає активних співробітників.")
        return
    keyboard = [[InlineKeyboardButton(f"{get_display_name(p)} ({p.get('role', '—')})", callback_data=f"fire_{uid}")] for uid, p in active]
    await update.message.reply_text("Оберіть співробітника для звільнення:", reply_markup=InlineKeyboardMarkup(keyboard))


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    asyncio.set_event_loop(asyncio.new_event_loop())
    app = Application.builder().token(BOT_TOKEN).build()

    job_queue = app.job_queue
    job_queue.run_daily(send_unresolved_digest, time=datetime.strptime("06:00", "%H:%M").time())
    job_queue.run_daily(send_birthday_greetings, time=datetime.strptime("06:00", "%H:%M").time())

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ASK_FIRST_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_first_name)],
            ASK_LAST_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_last_name)],
            ASK_BIRTHDAY: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_birthday)],
            ASK_PHONE: [MessageHandler(filters.CONTACT | (filters.TEXT & ~filters.COMMAND), ask_phone)],
            ASK_ROLE: [CallbackQueryHandler(ask_role_reg, pattern="^reg_role_")],
            ASK_RESTAURANT: [CallbackQueryHandler(ask_restaurant_reg, pattern="^reg_rest_")],
        },
        fallbacks=[CommandHandler("start", start)],
    )

    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("profile", show_profile))
    app.add_handler(CommandHandler("menu", show_admin_menu))
    app.add_handler(CommandHandler("staff", cmd_staff))
    app.add_handler(CommandHandler("confirm", cmd_confirm))
    app.add_handler(CommandHandler("fire", cmd_fire))
    app.add_handler(CallbackQueryHandler(handle_approve, pattern="^approve_"))
    app.add_handler(CallbackQueryHandler(handle_reject, pattern="^reject_"))
    app.add_handler(CallbackQueryHandler(handle_send_option, pattern="^send_"))
    app.add_handler(MessageHandler(filters.PHOTO, receive_photo))
    app.add_handler(CallbackQueryHandler(request_role_change, pattern="^request_role_change$"))
    app.add_handler(CallbackQueryHandler(handle_new_role, pattern="^newrole_"))
    app.add_handler(CallbackQueryHandler(handle_confirm_role, pattern="^confirmrole_"))
    app.add_handler(CallbackQueryHandler(handle_reject_role, pattern="^rejectrole_"))
    app.add_handler(CallbackQueryHandler(handle_fire, pattern="^fire_"))
    app.add_handler(CallbackQueryHandler(handle_restore, pattern="^restore_"))
    app.add_handler(CallbackQueryHandler(handle_status_update, pattern="^status_"))
    app.add_handler(CallbackQueryHandler(handle_assign, pattern="^assign_(?!to_|cancel_)"))
    app.add_handler(CallbackQueryHandler(handle_assign_to, pattern="^assignto_"))
    app.add_handler(CallbackQueryHandler(handle_assign_cancel, pattern="^assigncancel_"))
    app.add_handler(CallbackQueryHandler(handle_comment, pattern="^comment_(?!cancel_mode)"))
    app.add_handler(CallbackQueryHandler(handle_comment_cancel_mode, pattern="^comment_cancel_mode$"))
    app.add_handler(CallbackQueryHandler(handle_admin_menu, pattern="^menu_"))
    app.add_handler(CallbackQueryHandler(handle_admin_role_select, pattern="^adminrole_"))
    app.add_handler(CallbackQueryHandler(handle_admin_set_role, pattern="^adminsetrole_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receive_text))
    app.add_handler(MessageHandler(filters.VOICE, receive_voice))

    logger.info("Бот запущено...")
    app.run_polling()

if __name__ == "__main__":
    main()
