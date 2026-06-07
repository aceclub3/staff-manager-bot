import logging
import sys
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
from datetime import datetime, timedelta
from google.oauth2.service_account import Credentials
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters, ConversationHandler
)

import tempfile
import re
import uuid
import time
import shutil
import html
import threading
from telegram.error import BadRequest, RetryAfter, Forbidden, TimedOut, NetworkError
from telegram.ext import PicklePersistence
from telegram.helpers import escape_markdown

import storage  # єдина локальна база SQLite (feedback.db)

logging.basicConfig(stream=sys.stdout, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Ініціалізуємо БД і одноразово мігруємо старі JSON-стори (якщо таблиці порожні).
storage.init()
storage.migrate_kv_if_empty(os.path.dirname(__file__))


def md(s):
    """Екранує динамічний текст для legacy Markdown (parse_mode='Markdown')."""
    return escape_markdown(str(s if s is not None else ""), version=1)

BOT_TOKEN = os.getenv("FEEDBACK_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
SHEETS_CREDENTIALS = os.path.join(os.path.dirname(__file__), os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json"))
SPREADSHEET_ID = os.getenv("FEEDBACK_SPREADSHEET_ID")
OWNER_IDS = [int(x) for x in os.getenv("OWNER_IDS", "").split(",") if x]

RESTAURANTS = ["Терраса", "Хочу", "Хочу 2.0"]
ROLES = ["Офіціант", "Адміністратор", "Повар", "Шеф-повар", "Управляючий", "Виконавчий директор", "Технічний спеціаліст", "Клінінг"]
ADMIN_ROLES = {"Управляючий", "Виконавчий директор"}
KITCHEN_CATEGORIES = {"Кухня", "Закупки"}
CATEGORIES = ["Кухня", "Сервіс", "Техніка", "Закупки", "Гості", "Ідеї", "Чистота"]
CATEGORY_ICONS = {"Кухня": "🍽️", "Сервіс": "👥", "Техніка": "🔧", "Закупки": "🛒", "Гості": "💬", "Ідеї": "💡", "Чистота": "🧹"}
URGENCY_ICONS = {"Висока": "🔥", "Стандартна": "", "Низька": "🟢"}

# ─── МАРШРУТИЗАЦІЯ / ТЕРМІНОВІСТЬ / SLA / БЕКАП (аудит) ───────────────────────
GROUP_ROLES = {"Виконавчий директор"}            # бачать усі заклади (як власник)
VENUE_ROLES = {"Управляючий", "Адміністратор"}    # обмежені своїм закладом
REALTIME_URGENCY = {"Висока", "Стандартна"}       # реалтайм-пінг; "Низька" — лише в дайджесті
SLA_HOURS = {"Висока": 2, "Стандартна": 24, "Низька": 72}
ESCALATE_URGENCY = {"Висока"}                     # прострочені цих — ескалюємо власнику
BACKUP_DIRS = [r"E:\Denys\Feedback\Бекап", r"G:\Мой диск\BOTS\Feedback\Бекап"]
BACKUP_RETENTION = 21                             # скільки денних копій тримати

# ─── ПІДСУМКИ ЗМІН (рефлексивний відгук персоналу, окремо від задач) ──────────
SHIFT_REPORT_BTN = "📝 Підсумок зміни"
SHIFT_REPORTS_RETENTION_DAYS = 14      # старші за стільки днів — чистимо в дайджесті
SHIFT_REPORTS_TO_SHEET = False         # опц. дзеркало в окрему вкладку Google Sheets (owner вмикає)
SHIFT_REPORTS_SHEET_NAME = "Підсумки змін"

STATUS_PENDING = "pending"
STATUS_ACTIVE = "active"
STATUS_FIRED = "fired"

# ─── КОНСТАНТИ АВТОВИДАЛЕННЯ ──────────────────────────────────────────────────
TASK_ID_RE = re.compile(r'[A-Z]-\d{4}-\w+')   # T-0104-1 і подібні
AUTO_DELETE_DELAY = 30     # секунд — звичайні повідомлення
MENU_DELETE_DELAY = 120    # секунд — меню з кнопками
TASK_DELETE_DELAY = 86400  # секунд (24 год) — задачні повідомлення

PHOTO_PRIMARY_PATH = r"E:\Denys\Feedback\Фото"          # основне локальне сховище фото
PHOTO_MIRROR_PATH = r"G:\Мой диск\BOTS\Feedback\Фото"   # копія для перегляду в Google Drive

ASK_FIRST_NAME, ASK_LAST_NAME, ASK_BIRTHDAY, ASK_PHONE, ASK_ROLE, ASK_RESTAURANT = range(6)

openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ─── НАДІЙНЕ ЗБЕРІГАННЯ JSON (атомарний запис + відновлення) ──────────────────

def _atomic_write_json(path, data, ensure_ascii=False, indent=2):
    """Атомарний запис: tmp у тій самій теці -> fsync -> os.replace. Тримає .bak."""
    directory = os.path.dirname(path) or "."
    try:
        if os.path.exists(path):
            try:
                shutil.copy2(path, path + ".bak")
            except Exception:
                pass
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp_", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=ensure_ascii, indent=indent)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except Exception:
                    pass
    except Exception as e:
        logger.error(f"Atomic write failed for {path}: {e}")


def _safe_load_json(path, default):
    """Читає JSON; при пошкодженні пробує .bak, потім default. Ніколи не падає."""
    for candidate in (path, path + ".bak"):
        if os.path.exists(candidate):
            try:
                with open(candidate, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Load failed for {candidate}: {e}")
                if candidate == path:
                    # карантин пошкодженого файлу, щоб наступний кандидат (.bak) спрацював
                    try:
                        os.replace(path, path + ".corrupt")
                    except Exception:
                        pass
    return default() if callable(default) else default


# Профілі, msg_store, assign_store — тепер у SQLite (storage). In-memory кеші
# завантажуються при старті й пишуться наскрізь (write-through).

def load_profiles():
    data = storage.load_kv("profiles")
    try:
        return {int(k): v for k, v in data.items()}
    except Exception as e:
        logger.error(f"Profiles parse error: {e}")
        return {}

def save_profiles(profiles):
    storage.save_kv("profiles", profiles)

user_profiles = load_profiles()

# Кеш message_id для редагування сповіщень (у SQLite таблиця msgstore)
def load_msg_store():
    data = storage.load_kv("msgstore")
    # Одноразова нормалізація legacy-записів {uid: msg_id} -> {"ids":..., "text":"", "photo":None}
    changed = False
    for k, v in list(data.items()):
        if isinstance(v, dict) and "ids" not in v:
            data[k] = {"ids": v, "text": "", "photo": None}
            changed = True
    if changed:
        storage.save_kv("msgstore", data)
    return data

def save_msg_store(store):
    storage.save_kv("msgstore", store)

msg_store = load_msg_store()

# Доручення (таблиця assign)
def load_assign_store():
    return storage.load_kv("assign")

def save_assign_store(store):
    storage.save_kv("assign", store)

assign_store = load_assign_store()

# Канонічні задачі (джерело правди для дайджесту/переліку). Завантажуються тут,
# а одноразова міграція з Google Sheet (якщо БД порожня) — у _post_init.
tasks_store = storage.load_tasks()

# Підсумки змін (рефлексивний відгук). Окреме KV-сховище — НІКОЛИ не tasks_store.
# key = "telegram_id:YYYY-MM-DD"; value = {telegram_id, business_day, text, ...}.
reports_store = storage.load_kv("shiftreports")
# Кеш згенерованої Claude-сводки за день: {day: (count, text)} — щоб on-demand не кликав Claude повторно.
_shift_summary_cache = {}
# Хто вже отримав м'який нудж «лиши підсумок» сьогодні (in-memory; скидається при рестарті — ок).
_shift_nudged = {}

# ─── ПРОМПТ ───────────────────────────────────────────────────────────────────

PROMPT_FILE = os.path.join(os.path.dirname(__file__), "prompt.json")

DEFAULT_PROMPT = """Ти помічник для аналізу повідомлень персоналу ресторану.
Заклад відправника: <<restaurant>>
<<role_line>>Повідомлення: "<<message_text>>"

Якщо в повідомленні є кілька різних проблем — поверни масив об'єктів. Якщо одна — масив з одного об'єкта.

Поверни ТІЛЬКИ валідний JSON без пояснень та без markdown:
[{"category": "Кухня|Сервіс|Техніка|Закупки|Гості|Ідеї|Чистота",
  "summary": "текст повідомлення як є, виправ лише явні описки та заїкання",
  "restaurant": "Терраса|Хочу|Хочу 2.0",
  "responsible": "Шеф-повар|Управляючий|Виконавчий директор|Власник",
  "urgency": "Висока|Стандартна|Низька"}]

Правила summary:
- Зберігай оригінальний текст максимально близько до того, як написав співробітник
- Виправляй лише явні описки, повтори слів, заїкання
- НЕ перефразовуй, НЕ скорочуй, НЕ інтерпретуй
- Поганий приклад: "Бруд на підлозі" (скорочено і переінтерпретовано)
- Гарний приклад: "в залі брудна підлога біля четвертого столика, гості скаржаться" (як сказав співробітник)

Правила restaurant:
- Визнач, про який заклад йдеться в тексті повідомлення
- "Хочу 2.0", "Новий Хочу", "Хочу на Кабелянській" → "Хочу 2.0"
- "Хочу на Лисенку" → "Хочу"
- "Хочу" без уточнення → "Хочу"
- "Терраса" → "Терраса"
- Якщо заклад не згадується → "Терраса"

Правила urgency:
- Техніка зламалась → Висока
- Скарга гостя → Висока
- Ідея → Низька
- Решта → Стандартна

Категорія Чистота: бруд, прибирання, гігієна"""

def load_prompt():
    data = _safe_load_json(PROMPT_FILE, {})
    if isinstance(data, dict) and data.get("template"):
        return data["template"]
    return DEFAULT_PROMPT

def save_prompt(template):
    _atomic_write_json(PROMPT_FILE, {"template": template})

# ─── ТРЕКІНГ ПОВІДОМЛЕНЬ (для очищення перед дайджестом) ─────────────────────

# Трекінг повідомлень (таблиця chat) — для очищення чату перед дайджестом/переліком
def load_chat_msgs():
    return storage.load_kv("chat")

def save_chat_msgs_data(data):
    storage.save_kv("chat", data)

chat_msgs_store = load_chat_msgs()

def track_msg(chat_id, message_id):
    """Відстежує повідомлення бота для очищення перед дайджестом (per-key запис у БД)."""
    key = str(chat_id)
    if key not in chat_msgs_store:
        chat_msgs_store[key] = []
    if message_id not in chat_msgs_store[key]:
        chat_msgs_store[key].append(message_id)
    storage.kv_put("chat", key, chat_msgs_store[key])

async def delete_tracked_messages(bot, chat_ids):
    """Видаляє всі відстежені повідомлення бота перед дайджестом/переліком."""
    for chat_id in chat_ids:
        key = str(chat_id)
        for mid in list(chat_msgs_store.get(key, [])):
            try:
                await bot.delete_message(chat_id=chat_id, message_id=mid)
            except Exception:
                pass
        chat_msgs_store[key] = []
        storage.kv_put("chat", key, [])


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

RECIPIENT_ROLES = ADMIN_ROLES | VENUE_ROLES   # = {Управляючий, Виконавчий директор, Адміністратор}

def _profile_venues(p):
    """Заклади, які бачить/керує користувач. Пріоритет — налаштований у адмінці список
    `manages`; інакше — за роллю (Виконавчий директор/`all_restaurants` — усі; решта — свій
    заклад). Повертає множину назв закладів."""
    m = p.get("manages")
    if isinstance(m, list) and m:
        s = {v for v in m if v in RESTAURANTS}
        if s:
            return s
    if p.get("role") in GROUP_ROLES or p.get("all_restaurants"):
        return set(RESTAURANTS)
    r = p.get("restaurant")
    return {r} if r in RESTAURANTS else set(RESTAURANTS)

def get_recipients(category=None, restaurant=None):
    """Отримувачі сповіщення за роллю і ЗАКЛАДОМ (з урахуванням налаштованого `manages`).
    Власники — завжди. Керівні ролі — за своїми закладами. Шеф-повар — кухонні категорії."""
    recipients = set(OWNER_IDS)
    venue_match, venue_pool = set(), set()
    for uid, p in user_profiles.items():
        if p.get("status") != STATUS_ACTIVE:
            continue
        role = p.get("role", "")
        if role in RECIPIENT_ROLES:
            venue_pool.add(uid)
            if restaurant is None or restaurant in _profile_venues(p):
                venue_match.add(uid)
        elif role == "Шеф-повар" and category in KITCHEN_CATEGORIES:
            recipients.add(uid)
    recipients |= venue_match
    # Безхазяйний заклад (ніхто його не бачить) прикривають власники — лише логуємо.
    if restaurant is not None and not venue_match and venue_pool:
        logger.warning(f"No manager sees venue '{restaurant}' — covered by owners only")
    return recipients

def visible_restaurants(uid):
    """Які заклади користувач бачить у дайджесті/переліку (власник — усі)."""
    if uid in OWNER_IDS:
        return set(RESTAURANTS)
    return _profile_venues(user_profiles.get(uid, {}))

def can_act_on_task(uid, task_restaurant):
    """Чи може користувач діяти на задачі цього закладу (кнопки статусу/доручення/коментар)."""
    if uid in OWNER_IDS:
        return True
    p = user_profiles.get(uid, {})
    if p.get("status") != STATUS_ACTIVE:
        return False
    if p.get("role") in RECIPIENT_ROLES:
        return task_restaurant is None or task_restaurant in _profile_venues(p)
    return False

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


# ─── КЕШ КЛІЄНТА + НАДІЙНИЙ ДОСТУП ДО ТАБЛИЦІ ────────────────────────────────

SHEET_NAME = "Зворотний зв'язок"
SHEET_HEADER = ["Номер", "Дата", "Час", "Заклад", "Хто повідомив", "Роль", "Категорія",
                "Суть", "Відповідальний", "Терміновість", "Фото", "Статус", "Лог"]
REPORTS_SHEET_HEADER = ["Дата", "Час", "Заклад", "Хто", "Роль", "Анонім", "Джерело", "Текст"]
_gc = None
_ws = None
_reports_ws = None
_sheet_lock = threading.Lock()   # gspread/AuthorizedSession не потокобезпечні — серіалізуємо доступ

def _reset_sheets_cache():
    global _gc, _ws, _reports_ws
    _gc = None
    _ws = None
    _reports_ws = None

def _get_reports_worksheet():
    """Окрема вкладка для підсумків змін (створюється за потреби)."""
    global _gc, _reports_ws
    if _reports_ws is not None:
        return _reports_ws
    if _gc is None:
        _gc = get_sheets_client()
    ss = _gc.open_by_key(SPREADSHEET_ID)
    try:
        _reports_ws = ss.worksheet(SHIFT_REPORTS_SHEET_NAME)
    except gspread.WorksheetNotFound:
        _reports_ws = ss.add_worksheet(SHIFT_REPORTS_SHEET_NAME, rows=1000, cols=len(REPORTS_SHEET_HEADER))
        _reports_ws.append_row(REPORTS_SHEET_HEADER)
    return _reports_ws

def get_worksheet():
    """Повертає аркуш, кешуючи авторизований клієнт (без реавторизації на кожен виклик)."""
    global _gc, _ws
    if _ws is not None:
        return _ws
    if _gc is None:
        _gc = get_sheets_client()
    ss = _gc.open_by_key(SPREADSHEET_ID)
    try:
        _ws = ss.worksheet(SHEET_NAME)
    except gspread.WorksheetNotFound:
        _ws = ss.add_worksheet(SHEET_NAME, rows=1000, cols=13)
        _ws.append_row(SHEET_HEADER)
    return _ws

def _sheet_retry(fn, attempts=3):
    """Виконує синхронну операцію gspread з повтором і скиданням кешу при збої.
    Лок тримається ЛИШЕ навколо самого виклику (gspread-сесія не потокобезпечна),
    а backoff-пауза — поза локом, щоб не серіалізувати інші операції під час збою."""
    last = None
    for i in range(attempts):
        try:
            with _sheet_lock:
                return fn()
        except Exception as e:
            last = e
            logger.error(f"Sheets op failed (attempt {i+1}/{attempts}): {e}")
            _reset_sheets_cache()
            time.sleep(0.6 * (i + 1))
    raise last

async def _arun(fn):
    """Виконує блокуючу функцію в executor, щоб не блокувати event loop."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, fn)

def _read_records():
    """Стійке читання: get_all_values + нормалізація заголовків (не падає на дублях/порожніх)."""
    ws = get_worksheet()
    values = ws.get_all_values()
    if not values:
        return []
    headers = values[0]
    seen, norm = {}, []
    for i, h in enumerate(headers):
        h = (h or "").strip() or f"col{i+1}"
        if h in seen:
            seen[h] += 1
            h = f"{h}_{seen[h]}"
        else:
            seen[h] = 1
        norm.append(h)
    out = []
    for row in values[1:]:
        row = list(row) + [""] * (len(norm) - len(row))
        out.append({norm[i]: row[i] for i in range(len(norm))})
    return out

def get_main_keyboard(user_id):
    """Возвращает Reply-клавиатуру в зависимости от роли."""
    buttons = [[KeyboardButton("💬 Надіслати повідомлення")],
               [KeyboardButton(SHIFT_REPORT_BTN)]]   # підсумок зміни — доступний усім активним
    if can_manage_tasks(user_id):
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


# ─── СПІЛЬНИЙ BOT + СТІЙКА ВІДПРАВКА ──────────────────────────────────────────

application = None          # встановлюється у main()
_shared_bot = None          # резервний екземпляр, якщо application ще не готовий

def get_bot():
    """Повертає єдиний екземпляр Bot замість створення нового на кожен виклик."""
    global _shared_bot
    if application is not None:
        return application.bot
    if _shared_bot is None:
        from telegram import Bot
        _shared_bot = Bot(token=BOT_TOKEN)
    return _shared_bot


async def safe_send_message(bot, chat_id, text, reply_markup=None, parse_mode="Markdown", _retried=False):
    """Надсилає повідомлення; при помилці розбору Markdown повторює як звичайний
    текст, щоб повідомлення НІКОЛИ не зникало. Повертає Message або None."""
    try:
        return await bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode, reply_markup=reply_markup)
    except BadRequest as e:
        if parse_mode is not None:
            try:
                return await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
            except Exception as e2:
                logger.error(f"safe_send plain fallback failed {chat_id}: {e2}")
                return None
        logger.error(f"safe_send BadRequest {chat_id}: {e}")
        return None
    except RetryAfter as e:
        if not _retried:
            await asyncio.sleep(getattr(e, "retry_after", 1) + 0.5)
            return await safe_send_message(bot, chat_id, text, reply_markup, parse_mode, _retried=True)
        return None
    except Forbidden:
        return None  # користувач заблокував бота — не помилка
    except (TimedOut, NetworkError) as e:
        if not _retried:
            await asyncio.sleep(1)
            return await safe_send_message(bot, chat_id, text, reply_markup, parse_mode, _retried=True)
        logger.error(f"safe_send network fail {chat_id}: {e}")
        return None
    except Exception as e:
        logger.error(f"safe_send error {chat_id}: {e}")
        return None


async def safe_edit_message(bot, chat_id, message_id, text, reply_markup=None, parse_mode="Markdown"):
    """Редагує повідомлення; при помилці розбору Markdown повторює як звичайний
    текст. Повертає True якщо повідомлення вдалося оновити (або воно не змінилось)."""
    try:
        await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text,
                                    parse_mode=parse_mode, reply_markup=reply_markup)
        return True
    except BadRequest as e:
        msg = str(e).lower()
        if "not modified" in msg:
            return True
        if "parse" in msg or "entities" in msg:
            try:
                await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, reply_markup=reply_markup)
                return True
            except Exception as e2:
                logger.error(f"safe_edit plain fallback {chat_id}/{message_id}: {e2}")
                return False
        return False  # повідомлення не знайдено / не можна редагувати
    except RetryAfter as e:
        await asyncio.sleep(getattr(e, "retry_after", 1) + 0.5)
        try:
            await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text,
                                        parse_mode=parse_mode, reply_markup=reply_markup)
            return True
        except Exception:
            return False
    except Exception as e:
        logger.error(f"safe_edit error {chat_id}/{message_id}: {e}")
        return False


async def safe_send_photo(bot, chat_id, file_id=None, file_path=None, caption=None):
    """Надсилає фото за file_id; при невдачі — з локального файлу (G:). Повертає Message або None."""
    if file_id:
        for attempt in range(2):
            try:
                return await bot.send_photo(chat_id=chat_id, photo=file_id, caption=caption)
            except RetryAfter as e:
                await asyncio.sleep(getattr(e, "retry_after", 1) + 0.5)
            except Forbidden:
                return None
            except Exception as e:
                logger.error(f"send_photo by file_id failed {chat_id}: {e}")
                break
    if file_path and os.path.exists(file_path):
        try:
            with open(file_path, "rb") as fh:
                return await bot.send_photo(chat_id=chat_id, photo=fh, caption=caption)
        except Exception as e:
            logger.error(f"send_photo by path failed {chat_id}: {e}")
    return None

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
    _bot = get_bot()
    schedule_delete(context, _bot, update.effective_chat.id, update.message.message_id, delay=AUTO_DELETE_DELAY)
    if user_id in user_profiles:
        profile = user_profiles[user_id]
        status = profile.get("status")
        if status == STATUS_PENDING:
            msg = await update.message.reply_text("⏳ Ваша заявка на реєстрацію очікує підтвердження від керівника. Ми повідомимо вас як тільки її розглянуть.")
            track_msg(msg.chat_id, msg.message_id)
            schedule_delete(context, _bot, msg.chat_id, msg.message_id, delay=AUTO_DELETE_DELAY)
            return ConversationHandler.END
        if status == STATUS_FIRED:
            msg = await update.message.reply_text("❌ Ваш доступ відхилено. Зверніться до керівника.")
            track_msg(msg.chat_id, msg.message_id)
            schedule_delete(context, _bot, msg.chat_id, msg.message_id, delay=AUTO_DELETE_DELAY)
            return ConversationHandler.END
        if status == STATUS_ACTIVE:
            name = get_display_name(profile)
            msg = await update.message.reply_text(
                f"З поверненням, {name}! 👋\n\n"
                f"🏠 Заклад: {profile.get('restaurant', '—')}\n"
                f"💼 Роль: {profile.get('role', '—')}\n\n"
                "Надішліть повідомлення або скористайтесь кнопками.",
                reply_markup=get_main_keyboard(user_id)
            )
            track_msg(msg.chat_id, msg.message_id)
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
    bot = get_bot()
    full_name = md(get_display_name(profile))
    text = (
        f"🆕 *Нова заявка на реєстрацію*\n\n"
        f"👤 {full_name}\n💼 {md(profile.get('role'))}\n🏠 {md(profile.get('restaurant'))}\n"
        f"📱 {md(profile.get('phone', '—'))}\n🎂 {md(profile.get('birthday', '—'))}"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Підтвердити", callback_data=f"approve_{user_id}"),
        InlineKeyboardButton("❌ Відхилити", callback_data=f"reject_{user_id}"),
    ]])
    # реєстрацію закладу шлемо менеджерам цього закладу + власникам
    for admin_id in get_recipients(restaurant=profile.get("restaurant")):
        msg = await safe_send_message(bot, admin_id, text, reply_markup=keyboard, parse_mode="Markdown")
        if msg:
            track_msg(admin_id, msg.message_id)

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
    bot = get_bot()
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
    bot = get_bot()
    try:
        await bot.send_message(chat_id=user_id, text="❌ Вашу заявку на реєстрацію відхилено. Зверніться до керівника.")
    except Exception as e:
        logger.error(f"Reject notify error: {e}")


# ─── ОТРИМАННЯ ПОВІДОМЛЕНЬ ────────────────────────────────────────────────────

async def receive_text(update, context):
    user_id = update.effective_user.id
    text = update.message.text

    # Автовидалення повідомлення користувача
    _bot = get_bot()
    schedule_delete(context, _bot, update.effective_chat.id, update.message.message_id, delay=AUTO_DELETE_DELAY)

    if text == "👨‍💼 Адмін-меню":
        if can_manage_tasks(user_id):
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

    # Підсумок зміни: вхід у режим по кнопці
    if text == SHIFT_REPORT_BTN:
        await enter_shift_report_mode(update, context)
        return
    # Підсумок зміни: наступний текст у режимі — це і є звіт
    if context.user_data.get('report_mode'):
        await process_shift_report(update, context)
        return

    # Режим редагування промпту — приймаємо інструкцію ТЕКСТОМ (голос — у receive_voice)
    if context.user_data.get('prompt_editing') and is_owner(user_id):
        await process_prompt_text(update, context)
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
    _bot = get_bot()
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

    # Підсумок зміни: голосовий звіт у режимі підсумку
    if context.user_data.get('report_mode'):
        await process_shift_report(update, context)
        return

    # Коментар приймається лише текстом (process_comment читає update.message.text) —
    # інакше голос у режимі коментаря помилково створив би нову задачу.
    if context.user_data.get('commenting_on'):
        msg = await update.message.reply_text("💬 Коментар приймається лише текстом — напишіть його 👇")
        track_msg(msg.chat_id, msg.message_id)
        schedule_delete(context, _bot, msg.chat_id, msg.message_id, delay=AUTO_DELETE_DELAY)
        return

    processing_msg = await update.message.reply_text("🎙 Голосове отримано, обробляю...")
    track_msg(processing_msg.chat_id, processing_msg.message_id)
    schedule_delete(context, _bot, processing_msg.chat_id, processing_msg.message_id, delay=AUTO_DELETE_DELAY)
    voice_file = await update.message.voice.get_file()
    file_path = os.path.join(tempfile.gettempdir(), f"voice_{user_id}_{uuid.uuid4().hex[:8]}.ogg")
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
        "⏱ Автовідправка через 20 сек від вашого імені.\nАбо оберіть варіант:\n"
        "_🕵️ Анонімно: приховуємо лише ваше ім'я і роль. Заклад і суть керівники бачать "
        "(кухонні питання бачить і шеф-кухар)._",
        reply_markup=keyboard, parse_mode="Markdown"
    )
    context.user_data['option_message_id'] = msg.message_id
    context.user_data['option_chat_id'] = msg.chat_id
    context.user_data['waiting_for_option'] = True

    bot = get_bot()
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

    bot = get_bot()

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

    _bot = get_bot()

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
        cancel_user_jobs(context, user_id, "photowait")
        context.job_queue.run_once(
            photo_waiting_clear_callback,
            when=MENU_DELETE_DELAY,
            name=f"photowait_{user_id}",
            data={"user_id": user_id}
        )


async def receive_photo(update, context):
    """Обработчик входящих фото."""
    user_id = update.effective_user.id

    if user_id not in user_profiles or user_profiles[user_id].get("status") != STATUS_ACTIVE:
        return

    # Автовидалення фото користувача
    _bot = get_bot()
    schedule_delete(context, _bot, update.effective_chat.id, update.message.message_id, delay=AUTO_DELETE_DELAY)

    # У режимі підсумку зміни фото не приймаємо (лише текст/голос); режим лишається активним.
    if context.user_data.get('report_mode'):
        msg = await update.message.reply_text(
            "📝 Підсумок зміни приймає лише текст або голос. Надішліть текстом або голосом 👇")
        track_msg(msg.chat_id, msg.message_id)
        schedule_delete(context, _bot, msg.chat_id, msg.message_id, delay=AUTO_DELETE_DELAY)
        return

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
        bot = get_bot()
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
        bot = get_bot()
        try:
            sent = await bot.send_message(chat_id=chat_id, text="⏱ Час вийшов. Фото скинуто. Надішліть повідомлення знову.")
            # Видаляємо системне повідомлення через 30 сек
            schedule_delete(context, bot, chat_id, sent.message_id, delay=30)
        except Exception:
            pass

async def photo_waiting_clear_callback(context):
    """Скидає waiting_for_photo якщо фото так і не надійшло після кнопки 'Додати фото'."""
    user_id = context.job.data["user_id"]
    user_data = context.application.user_data.get(user_id, {})
    if user_data.get('waiting_for_photo'):
        user_data.pop('waiting_for_photo', None)
        user_data.pop('pending_message', None)
        user_data.pop('message_type', None)


async def save_photo_to_gdrive(file_id: str, filename: str) -> str:
    """Завантажує фото з Telegram ОДИН раз і зберігає НЕЗАЛЕЖНО на основне сховище (E:)
    та копію на Google Drive (G:). Падіння одного диска не блокує інший. Повертає шлях
    ПЕРШОГО успішно збереженого файлу (E: у пріоритеті) для локального резерву, або ''
    (доставка фото все одно тримається на Telegram file_id незалежно від диска)."""
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        bot = get_bot()
        tg_file = await bot.get_file(file_id)
        data = bytes(await tg_file.download_as_bytearray())
    except Exception as e:
        logger.error(f"Photo download error: {e}")
        return ""
    saved_path = ""
    for label, base in (("primary E:", PHOTO_PRIMARY_PATH), ("mirror G:", PHOTO_MIRROR_PATH)):
        try:
            target_dir = os.path.join(base, today)
            os.makedirs(target_dir, exist_ok=True)
            path = os.path.join(target_dir, filename)
            with open(path, "wb") as fh:
                fh.write(data)
            logger.info(f"Photo saved ({label}): {path}")
            if not saved_path:
                saved_path = path   # E: у пріоритеті як шлях резерву
        except Exception as e:
            logger.error(f"Photo save to {label} failed: {e}")
    return saved_path


def _store_task_photo(fid, file_id, path):
    """Зберігає фото у msgstore без живої відправки (для Низької — щоб дайджест міг показати)."""
    entry = msg_store.get(fid)
    if not (isinstance(entry, dict) and "ids" in entry):
        entry = {"ids": {}, "text": "", "photo": None, "photo_path": ""}
    entry["photo"] = file_id
    entry["photo_path"] = path or entry.get("photo_path", "")
    msg_store[fid] = entry
    storage.kv_put("msgstore", fid, entry)


async def _save_and_notify(results, user_data, profile, photo_file_id, context):
    """Зберігає фото на диск (раз), створює задачі та (для термінових) розсилає сповіщення.
    Низька терміновість — БЕЗ реалтайм-пінгу (лише в ранковому зведенні), щоб не перевантажувати.
    Повертає (categories, feedback_ids, delivery[])."""
    gdrive_path = ""
    if photo_file_id:
        filename = f"feedback_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:4]}.jpg"
        gdrive_path = await save_photo_to_gdrive(photo_file_id, filename)
    categories, ids, delivery = [], [], []
    first_photo_sent = False
    for result in results:
        feedback_id = create_task(result, user_data, profile, has_photos=bool(photo_file_id))
        urgency = result.get("urgency", "Стандартна")
        realtime = urgency in REALTIME_URGENCY
        reached = 0
        if realtime:
            send_live = bool(photo_file_id) and not first_photo_sent
            reached = await send_notifications(
                result, user_data, profile, photo_file_id=photo_file_id, feedback_id=feedback_id,
                context=context, photo_path=gdrive_path, send_live_photo=send_live) or 0
            if send_live and reached:
                first_photo_sent = True
        elif photo_file_id:
            _store_task_photo(feedback_id, photo_file_id, gdrive_path)   # для дайджесту
        categories.append(result.get('category', '—'))
        ids.append(feedback_id)
        delivery.append({"urgency": urgency, "reached": reached, "realtime": realtime})
    return categories, ids, delivery


def _build_confirm_text(results, is_anonymous, photo_file_id, ids, delivery=None):
    anon_note = " (анонімно)" if is_anonymous else ""
    count_note = f" ({len(results)} проблеми)" if len(results) > 1 else ""
    photo_note = "\n📷 З фото" if photo_file_id else ""
    note = ""
    if delivery:
        realtime = [d for d in delivery if d["realtime"]]
        digestonly = [d for d in delivery if not d["realtime"]]
        if realtime and all(d["reached"] == 0 for d in realtime):
            note = "\n⏳ Керівники зараз поза мережею — побачать у зведенні."
        elif digestonly and not realtime:
            note = "\n🟢 Зʼявиться у ранковому зведенні керівникам."
    result_lines = []
    for i, r in enumerate(results):
        if i > 0:
            result_lines.append("")
        icon = CATEGORY_ICONS.get(r.get('category', '—'), '📌')
        result_lines.append(f"{icon} {r.get('category', '—')} · {r.get('urgency', '—')}")
        result_lines.append(r.get('summary', '—'))
    return (
        f"✅ Повідомлення прийнято{anon_note}!{count_note}\n\n"
        f"━━━━━━━━━━━━━━━\n"
        + "\n".join(result_lines)
        + f"\n━━━━━━━━━━━━━━━{photo_note}{note}\n\nДякуємо за зворотний зв'язок 🙏"
    )


async def process_message(query_or_update, context, profile, use_message=False):
    # Атомарне «захоплення» pending_message ДО будь-якого await — щоб автовідправка (20с)
    # і натискання кнопки не створили дубль задачі: хто другий, той отримає None і вийде.
    pending = context.user_data.pop('pending_message', None)
    message_type = context.user_data.pop('message_type', None)
    if pending is None:
        return
    target = query_or_update.message
    text = await transcribe_voice(pending) if message_type == 'voice' else pending
    if not text:
        # Фото НЕ скидаємо — даємо шанс повторити опис; перезапускаємо 2-хв таймер очищення.
        _uid = profile.get('telegram_id')
        if context.user_data.get('pending_photo') and _uid:
            cancel_user_jobs(context, _uid, "photoclear")
            context.job_queue.run_once(photo_clear_callback, when=120, name=f"photoclear_{_uid}",
                                       data={"user_id": _uid, "chat_id": target.chat_id})
        await target.reply_text("❌ Не вдалося обробити повідомлення. Спробуйте ще раз.")
        return

    photo_file_id = context.user_data.pop('pending_photo', None)
    is_anonymous = context.user_data.get('is_anonymous', False)

    results = await analyze_with_claude(text, profile, is_anonymous=is_anonymous)
    categories, ids, delivery = await _save_and_notify(results, context.user_data, profile, photo_file_id, context)

    for key in ['waiting_for_option', 'waiting_for_photo', 'option_message_id', 'option_chat_id']:
        context.user_data.pop(key, None)

    _uid = profile.get('telegram_id')
    nudge = _shift_nudge_line() if _should_shift_nudge(_uid) else ""
    confirm_text = _build_confirm_text(results, is_anonymous, photo_file_id, ids, delivery) + nudge
    # БЕЗ reply_markup: це підтвердження автовидаляється (нижче). Reply-клавіатура
    # «прив'язана» до останнього повідомлення-носія, тож видалення носія прибрало б
    # постійні кнопки (зокрема «Адмін-меню»). Клавіатура й так живе на /start і в дайджесті.
    sent = await target.reply_text(confirm_text)
    if sent and nudge:
        _mark_shift_nudged(_uid)
    # Видаляємо підтвердження через 90 сек
    schedule_delete(context, get_bot(), sent.chat_id, sent.message_id, delay=90)

async def _do_process(bot, chat_id, user_id, user_data, profile, context=None):
    """Вспомогательная функция для автоотправки (без query объекта)."""
    # Атомарне «захоплення» (див. process_message) — проти дубля задачі від кнопки+автовідправки.
    pending = user_data.pop('pending_message', None)
    message_type = user_data.pop('message_type', None)
    if pending is None:
        return
    text = await transcribe_voice(pending) if message_type == 'voice' else pending
    if not text:
        if user_data.get('pending_photo') and context:
            cancel_user_jobs(context, user_id, "photoclear")
            context.job_queue.run_once(photo_clear_callback, when=120, name=f"photoclear_{user_id}",
                                       data={"user_id": user_id, "chat_id": chat_id})
        await safe_send_message(bot, chat_id, "❌ Не вдалося обробити повідомлення.", parse_mode=None)
        return

    photo_file_id = user_data.pop('pending_photo', None)
    is_anonymous = user_data.get('is_anonymous', False)

    results = await analyze_with_claude(text, profile, is_anonymous=is_anonymous)
    categories, ids, delivery = await _save_and_notify(results, user_data, profile, photo_file_id, context)

    for key in ['waiting_for_option', 'waiting_for_photo']:
        user_data.pop(key, None)

    nudge = _shift_nudge_line() if _should_shift_nudge(user_id) else ""
    confirm_text = _build_confirm_text(results, is_anonymous, photo_file_id, ids, delivery) + nudge
    # БЕЗ reply_markup — див. коментар у process(): носій reply-клавіатури не можна автовидаляти.
    sent = await safe_send_message(bot, chat_id, confirm_text, parse_mode=None)
    if sent:
        if nudge:
            _mark_shift_nudged(user_id)
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

def _retry_sync(fn, attempts=2, delay=1.0):
    last = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:
            last = e
            logger.error(f"Claude call failed (attempt {i+1}/{attempts}): {e}")
            time.sleep(delay * (i + 1))
    raise last

def _strip_code_fences(s):
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z0-9]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    return s.strip()

def _extract_json(s):
    """Витягує JSON навіть якщо модель обгорнула його у ```json або додала текст."""
    s = _strip_code_fences(s)
    try:
        return json.loads(s)
    except Exception:
        pass
    for open_c, close_c in (("[", "]"), ("{", "}")):
        start, end = s.find(open_c), s.rfind(close_c)
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(s[start:end + 1])
            except Exception:
                continue
    return None

def _call_claude_sync(prompt, max_tokens=800):
    return claude_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )

async def analyze_with_claude(text, profile, is_anonymous=False):
    fallback_restaurant = profile.get('restaurant', 'Терраса') or 'Терраса'

    def _fallback():
        # ВЕСЬ текст як summary (без обрізання до 50 символів) + коректний заклад
        return [{"category": "Інше", "summary": text, "responsible": "Управляючий",
                 "urgency": "Стандартна", "restaurant": fallback_restaurant}]

    try:
        role_line = "" if is_anonymous else f"Роль відправника: {profile.get('role', 'Невідомо')}\n"
        template = load_prompt()
        prompt = (template
            .replace("<<restaurant>>", profile.get('restaurant', 'Невідомо'))
            .replace("<<role_line>>", role_line)
            .replace("<<message_text>>", text))
        response = await _arun(lambda: _retry_sync(lambda: _call_claude_sync(prompt)))
        raw = response.content[0].text.strip()
    except Exception as e:
        logger.error(f"Claude API error: {e}")
        return _fallback()

    parsed = _extract_json(raw)
    if parsed is None:
        logger.error("Claude JSON parse failed; using raw text as summary")
        return _fallback()
    if isinstance(parsed, dict):
        parsed = [parsed]

    norm = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        cat = item.get("category", "Інше")
        if cat not in CATEGORIES and cat != "Інше":
            cat = "Інше"
        item["category"] = cat
        if not item.get("restaurant"):
            item["restaurant"] = fallback_restaurant
        if not item.get("summary"):
            item["summary"] = text
        norm.append(item)
    return norm if norm else _fallback()


# ─── GOOGLE SHEETS ────────────────────────────────────────────────────────────

def get_msg_data(feedback_id):
    """Повертає (msg_ids dict, original_text) з msg_store."""
    data = msg_store.get(feedback_id, {})
    if isinstance(data, dict) and "ids" in data:
        return data["ids"], data.get("text", "")
    return data, ""

async def edit_all_messages(bot, feedback_id, new_text, keyboard):
    """Редагує повідомлення у всіх отримувачів і оновлює збережений текст.
    Текст зберігається лише якщо хоч одне редагування вдалося — щоб некоректний
    Markdown не «отруїв» збережений текст і не сховав задачу в наступному дайджесті."""
    msg_ids, _ = get_msg_data(feedback_id)
    any_ok = False
    for uid_str, message_id in list(msg_ids.items()):
        ids = message_id if isinstance(message_id, list) else [message_id]
        for mid in ids:
            ok = await safe_edit_message(bot, int(uid_str), mid, new_text,
                                         reply_markup=keyboard, parse_mode="Markdown")
            any_ok = any_ok or ok
    if any_ok and feedback_id in msg_store and isinstance(msg_store[feedback_id], dict):
        msg_store[feedback_id]["text"] = new_text
        storage.kv_put("msgstore", feedback_id, msg_store[feedback_id])

def get_restaurant_prefix(restaurant):
    prefixes = {"Терраса": "T", "Хочу": "H", "Хочу 2.0": "H2"}
    return prefixes.get(restaurant, "X")

def generate_feedback_id(restaurant):
    """Генерує ID виду T-0104-1, рахуючи задачі цього закладу за сьогодні в БД."""
    prefix = get_restaurant_prefix(restaurant)
    today = datetime.now().strftime("%d%m")
    id_prefix = f"{prefix}-{today}-"
    next_num = sum(1 for k in tasks_store if str(k).startswith(id_prefix)) + 1
    return f"{id_prefix}{next_num}"

def _task_record(rec):
    """Перетворює нативний запис задачі на dict з укр. ключами (для build_row_text/дайджесту)."""
    return {
        "Номер": rec.get("fid", ""), "Дата": rec.get("created_date", ""), "Час": rec.get("created_time", ""),
        "Заклад": rec.get("restaurant", ""), "Хто повідомив": rec.get("sender_name", "—"),
        "Роль": rec.get("sender_role", "—"), "Категорія": rec.get("category", "—"),
        "Суть": rec.get("summary", "—"), "Відповідальний": rec.get("responsible", "—"),
        "Терміновість": rec.get("urgency", "—"),
        "Фото": "є фото" if rec.get("has_photo") else "—",
        "Статус": rec.get("status", "Нове"), "Лог": rec.get("log", "") or "",
        "_ts": rec.get("created_ts") or "", "_ack": rec.get("acknowledged_at") or "",
        "_completed": rec.get("completed_date") or "",
    }

def _urg_rank(u):
    return {"Висока": 0, "Стандартна": 1, "Низька": 2}.get(str(u), 1)

def _task_age_hours(rec):
    """Вік задачі в годинах (за created_ts, фолбек — created_date+time)."""
    ts = rec.get("created_ts")
    try:
        if ts:
            dt = datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
        else:
            dt = datetime.strptime(f"{rec.get('created_date','')} {rec.get('created_time','00:00')}", "%d.%m.%Y %H:%M")
        return max(0.0, (datetime.now() - dt).total_seconds() / 3600.0)
    except Exception:
        return 0.0

def create_task(result, user_data, profile, has_photos=False):
    """СТВОРЮЄ задачу в SQLite (джерело правди) і дзеркалить у Google Sheets (фоном)."""
    restaurant = result.get("restaurant", "Терраса")
    fid = generate_feedback_id(restaurant)
    now = datetime.now()
    reporter_id = None if user_data.get("is_anonymous") else profile.get("telegram_id")
    rec = {
        "fid": fid,
        "created_date": now.strftime("%d.%m.%Y"), "created_time": now.strftime("%H:%M"),
        "created_ts": now.strftime("%Y-%m-%d %H:%M:%S"),
        "restaurant": restaurant,
        "sender_name": user_data.get("sender_name", "—"),
        "sender_role": user_data.get("sender_role", "—"),
        "reporter_id": reporter_id,
        "category": result.get("category", "—"),
        "summary": result.get("summary", "—"),
        "responsible": result.get("responsible", "—"),
        "urgency": result.get("urgency", "—"),
        "has_photo": bool(has_photos),
        "status": "Нове", "log": "",
        "acknowledged_at": None, "completed_date": None, "escalated": 0, "assignee_id": None,
        "seq": storage.next_seq(),
    }
    tasks_store[fid] = rec
    storage.save_task(rec)
    asyncio.create_task(_mirror_create_to_sheet(rec))
    logger.info(f"Task created {fid}: {rec['category']} / {rec['urgency']} / {restaurant}")
    return fid

def set_task_status(feedback_id, status):
    """Оновлює статус задачі в SQLite (джерело правди) + дзеркало в Sheets (фоном).
    Веде acknowledged_at (взято в роботу), completed_date (закрито) і escalated (скидаємо при перевідкритті)."""
    rec = tasks_store.get(feedback_id)
    if rec:
        rec["status"] = status
        if _is_resolved(status):
            rec["completed_date"] = datetime.now().strftime("%d.%m.%Y")
        else:
            rec["completed_date"] = None          # перевідкрито
            rec["escalated"] = 0                   # знову можна ескалювати
        if str(status).startswith("В роботі") and not rec.get("acknowledged_at"):
            rec["acknowledged_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        storage.save_task(rec)
    asyncio.create_task(_mirror_status_to_sheet(feedback_id, status))

def add_task_log(feedback_id, entry):
    """Додає запис у Лог задачі в SQLite + дзеркало в Sheets (фоном)."""
    rec = tasks_store.get(feedback_id)
    if rec:
        cur = rec.get("log", "")
        rec["log"] = (cur + " | " + entry).strip(" | ")
        storage.save_task(rec)
    asyncio.create_task(_mirror_log_to_sheet(feedback_id, entry))

# ─── ДЗЕРКАЛО В GOOGLE SHEETS (best-effort, не впливає на джерело правди) ──────

async def _mirror_create_to_sheet(rec):
    def _do():
        ws = get_worksheet()
        ws.insert_row([
            rec["fid"], rec["created_date"], rec["created_time"], rec["restaurant"],
            rec["sender_name"], rec["sender_role"], rec["category"], rec["summary"],
            rec["responsible"], rec["urgency"],
            "є фото" if rec.get("has_photo") else "—", rec.get("status", "Нове"),
            rec.get("log", "") or "Нове",
        ], index=2)
    try:
        await _arun(lambda: _sheet_retry(_do))
    except Exception as e:
        logger.error(f"Sheet mirror (create) failed {rec.get('fid')}: {e}")

async def _mirror_status_to_sheet(feedback_id, status):
    if not feedback_id or str(feedback_id).startswith("LOCAL-"):
        return
    def _do():
        ws = get_worksheet()
        all_ids = ws.col_values(1)
        for i, val in enumerate(all_ids):
            if val == feedback_id:
                ws.update_cell(i + 1, 12, status)
                return
    try:
        await _arun(lambda: _sheet_retry(_do))
    except Exception as e:
        logger.error(f"Sheet mirror (status) failed {feedback_id}: {e}")

async def _mirror_log_to_sheet(feedback_id, entry):
    if not feedback_id or str(feedback_id).startswith("LOCAL-"):
        return
    def _do():
        ws = get_worksheet()
        all_ids = ws.col_values(1)
        for i, val in enumerate(all_ids):
            if val == feedback_id:
                current = ws.cell(i + 1, 13).value or ""
                ws.update_cell(i + 1, 13, (current + " | " + entry).strip(" | "))
                return
    try:
        await _arun(lambda: _sheet_retry(_do))
    except Exception as e:
        logger.error(f"Sheet mirror (log) failed {feedback_id}: {e}")

async def send_notifications(result, user_data, profile, photo_file_id=None, feedback_id=None,
                             context=None, photo_path="", send_live_photo=True):
    try:
        bot = get_bot()
        category = result.get("category", "—")
        icon = CATEGORY_ICONS.get(category, "📌")
        sender = user_data.get("sender_name", "Аноним")
        now = datetime.now()

        safe_id = (feedback_id or "").replace("-", "_")
        id_str = f"`{feedback_id}`" if feedback_id else ""

        restaurant = result.get("restaurant", "Терраса")
        urg = result.get("urgency", "Стандартна")
        urg_icon = URGENCY_ICONS.get(urg, "")
        text = (
            f"📌 {id_str} · {md(restaurant)} · {icon} *{md(category)}*{(' ' + urg_icon) if urg_icon else ''}\n"
            f"{md(result.get('summary', '—'))}\n"
            f"_{now.strftime('%d.%m %H:%M')} · {md(sender)}_"
        )

        keyboard = build_task_keyboard(safe_id)
        recipients = get_recipients(category, restaurant)   # маршрут за закладом
        msg_ids = {}

        logger.info(f"Sending notifications: {feedback_id} {restaurant}/{category}/{urg}, recipients={len(recipients)}, photo={'yes' if photo_file_id else 'no'}")

        for uid in recipients:
            msg = await safe_send_message(bot, uid, text, reply_markup=keyboard, parse_mode="Markdown")
            if msg is None:
                continue
            msg_ids[str(uid)] = msg.message_id
            track_msg(uid, msg.message_id)
            if context:
                schedule_delete(context, bot, uid, msg.message_id, delay=TASK_DELETE_DELAY)

            # Живе фото шлемо лише для першої підзадачі (щоб не дублювати), але file_id
            # зберігаємо для КОЖНОЇ задачі нижче — щоб дайджест міг показати фото будь-якій.
            if photo_file_id and send_live_photo:
                photo_msg = await safe_send_photo(bot, uid, file_id=photo_file_id, file_path=photo_path)
                if photo_msg is not None:
                    track_msg(uid, photo_msg.message_id)
                    if context:
                        schedule_delete(context, bot, uid, photo_msg.message_id, delay=TASK_DELETE_DELAY)

        # Зберігаємо ЗАВЖДИ, коли є feedback_id (навіть якщо жодна відправка не вдалась) —
        # щоб текст і фото можна було відновити в дайджесті / ручному переліку.
        if feedback_id:
            entry = msg_store.get(feedback_id)
            if not (isinstance(entry, dict) and "ids" in entry):
                entry = {"ids": {}, "text": text, "photo": None, "photo_path": ""}
            entry["ids"].update(msg_ids)
            entry["text"] = text
            if photo_file_id:
                entry["photo"] = photo_file_id
                entry["photo_path"] = photo_path or entry.get("photo_path", "")
            msg_store[feedback_id] = entry
            storage.kv_put("msgstore", feedback_id, entry)   # лише один ключ (без перезапису всієї таблиці)
        return len(msg_ids)

    except Exception as e:
        logger.error(f"Notification error: {e}")
        return 0


# ─── СТАТУСИ ЗАДАЧ ────────────────────────────────────────────────────────────

async def _notify_reporter(rec, actor_id, text):
    """Сповіщає автора задачі про результат (закриття петлі). Поважає анонімність
    (reporter_id=None для анонімних), не пінгує самого виконавця."""
    rid = rec.get("reporter_id") if isinstance(rec, dict) else None
    if not rid or rid == actor_id:
        return
    try:
        await safe_send_message(get_bot(), rid, text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"reporter notify failed: {e}")


# ─── ЯДРО ДІЙ НАД ЗАДАЧЕЮ (спільне для кнопок чату і дашборду «Пульт керівника») ──
# Ці корутини НЕ залежать від telegram Update/CallbackQuery — приймають лише прості
# аргументи. Завдяки цьому дашборд (webapp_api) виконує ТОЧНО ті самі дії, що й кнопки
# в чаті: edit_all_messages оновлює текст у всіх отримувачів, _notify_reporter закриває
# петлю з автором, дзеркало в Sheets пишеться — без дублювання логіки й розсинхрону.
# Повертають dict {ok: bool, error?: str, ...}. Перевірку прав/ідемпотентності роблять
# усередині, тож і кнопка, і дашборд гейтяться однаково.

def _fallback_task_text(feedback_id):
    """Базовий текст задачі, коли в msgstore немає збереженого (рідко: дія по старій
    задачі без живої копії). Формат як у send_notifications."""
    rec = tasks_store.get(feedback_id, {})
    if not rec:
        return ""
    category = rec.get("category", "—")
    icon = CATEGORY_ICONS.get(category, "📌")
    urg = rec.get("urgency", "Стандартна")
    urg_icon = URGENCY_ICONS.get(urg, "")
    return (f"📌 `{feedback_id}` · {md(rec.get('restaurant',''))} · {icon} *{md(category)}*"
            f"{(' ' + urg_icon) if urg_icon else ''}\n{md(rec.get('summary','—'))}")

def _is_task_assignee(feedback_id, user_id):
    """Активний виконавець цієї задачі (має права на її кнопки попри інший заклад)."""
    info = assign_store.get(feedback_id, {})
    return (info.get("assignee_id") == user_id
            and user_profiles.get(user_id, {}).get("status") == STATUS_ACTIVE)

async def task_action_status(feedback_id, action, actor_id, fallback_text=None):
    """done / wip / cancel. Перевіряє права та ідемпотентність. {ok, error?, status?}."""
    rec = tasks_store.get(feedback_id, {})
    if not can_act_on_task(actor_id, rec.get("restaurant")) and not _is_task_assignee(feedback_id, actor_id):
        return {"ok": False, "error": "no_perm"}
    if action == "wip" and _is_resolved(rec.get("status", "")):
        return {"ok": False, "error": "already_closed", "status": rec.get("status", "")}
    profile = user_profiles.get(actor_id, {})
    who = get_display_name(profile)
    now = datetime.now().strftime("%d.%m %H:%M")
    assigned = feedback_id in assign_store
    safe_id = feedback_id.replace("-", "_")
    if action == "done":
        status_text = f"✅ Виконано — {md(who)} {now}"
        new_keyboard = build_task_keyboard(safe_id, assigned=assigned, done=True)
        sheet_status = f"Виконано — {who}"
        history_entry = f"✅ Виконано — {who} {now}"
        # завершену задачу знімаємо з доручення (щоб 'Доручено' не висіло вічно)
        if feedback_id in assign_store:
            assign_store.pop(feedback_id, None)
            save_assign_store(assign_store)
    elif action == "wip":
        status_text = f"🔄 В роботі — {md(who)} {now}"
        new_keyboard = build_wip_keyboard(safe_id, assigned=assigned)
        sheet_status = f"В роботі — {who}"
        history_entry = f"🔄 В роботі — {who} {now}"
    elif action == "cancel":
        status_text = f"↩️ Скасовано — {md(who)} {now}"
        new_keyboard = build_task_keyboard(safe_id, assigned=assigned)
        sheet_status = "Нове"
        history_entry = f"↩️ Скасовано — {who} {now}"
    else:
        return {"ok": False, "error": "bad_action"}
    _, stored_text = get_msg_data(feedback_id)
    base_text = stored_text if stored_text else (fallback_text or _fallback_task_text(feedback_id))
    # прибираємо лише попередні СТАТУСНІ рядки (за повним префіксом бота),
    # щоб випадково не вирізати опис, який міг початись з ✅/🔄/↩️
    status_prefixes = ("✅ Виконано", "🔄 В роботі", "↩️ Скасовано")
    base_lines = [l for l in base_text.split("\n") if not l.lstrip().startswith(status_prefixes)]
    new_text = "\n".join(base_lines).rstrip() + f"\n{status_text}"
    bot = get_bot()
    await edit_all_messages(bot, feedback_id, new_text, new_keyboard)
    set_task_status(feedback_id, sheet_status)
    add_task_log(feedback_id, history_entry)
    if action == "done":
        await _notify_reporter(rec, actor_id, f"✅ Вашу задачу `{feedback_id}` виконано — {md(who)}")
    return {"ok": True, "status_text": status_text}

async def task_action_delete(feedback_id, actor_id):
    """Видалення задачі (лише власник): прибирає живі повідомлення, чистить кеші, закриває петлю."""
    if not is_owner(actor_id):
        return {"ok": False, "error": "owner_only"}
    rec = tasks_store.get(feedback_id, {})
    who = get_display_name(user_profiles.get(actor_id, {}))
    now = datetime.now().strftime("%d.%m %H:%M")
    bot = get_bot()
    msg_ids, _ = get_msg_data(feedback_id)
    for uid_str, mid in list(msg_ids.items()):
        ids = mid if isinstance(mid, list) else [mid]
        for m in ids:
            try:
                await bot.delete_message(chat_id=int(uid_str), message_id=m)
            except Exception:
                pass
    if feedback_id in msg_store:
        del msg_store[feedback_id]
        storage.kv_delete("msgstore", feedback_id)
    if feedback_id in assign_store:
        assign_store.pop(feedback_id, None)
        save_assign_store(assign_store)
    set_task_status(feedback_id, f"Видалено — {who}")
    add_task_log(feedback_id, f"🗑️ Видалено — {who} {now}")
    await _notify_reporter(rec, actor_id, f"🗑️ Вашу задачу `{feedback_id}` закрито керівником.")
    return {"ok": True}

async def task_action_comment(feedback_id, actor_id, comment_text):
    """Дописує коментар у задачу (перевірка прав; для кнопки і дашборду)."""
    rec = tasks_store.get(feedback_id, {})
    if not can_act_on_task(actor_id, rec.get("restaurant")) and not _is_task_assignee(feedback_id, actor_id):
        return {"ok": False, "error": "no_perm"}
    comment_text = (comment_text or "").strip()
    if not comment_text:
        return {"ok": False, "error": "empty"}
    who = get_display_name(user_profiles.get(actor_id, {}))
    now = datetime.now().strftime("%d.%m %H:%M")
    comment_line_raw = f"💬 {who} {now}: {comment_text}"          # для таблиці (без екранування)
    comment_line = f"💬 {md(who)} {now}: {md(comment_text)}"      # для показу в Telegram
    _, stored_text = get_msg_data(feedback_id)
    new_text = (stored_text + f"\n{comment_line}") if stored_text else comment_line
    assigned = feedback_id in assign_store
    safe_id = feedback_id.replace("-", "_")
    keyboard = build_task_keyboard(safe_id, assigned=assigned)
    bot = get_bot()
    await edit_all_messages(bot, feedback_id, new_text, keyboard)
    add_task_log(feedback_id, comment_line_raw)
    return {"ok": True}

async def task_action_assign(feedback_id, target_uid, actor_id, context=None, fallback_text=None):
    """Доручає задачу target_uid (перевірка прав). Спільне для кнопки і дашборду.
    context — опц. (для автовидалення повідомлень); кнопковий хендлер сам показує підтвердження."""
    rec = tasks_store.get(feedback_id, {})
    if not can_act_on_task(actor_id, rec.get("restaurant")):
        return {"ok": False, "error": "no_perm"}
    target_profile = user_profiles.get(target_uid)
    if not target_profile or target_profile.get("status") != STATUS_ACTIVE:
        return {"ok": False, "error": "bad_target"}
    safe_id = feedback_id.replace("-", "_")
    # денормалізуємо виконавця в задачу (для статистики/ескалації)
    if rec:
        rec["assignee_id"] = target_uid
        storage.save_task(rec)
    assigner_name = get_display_name(user_profiles.get(actor_id, {}))
    target_name = get_display_name(target_profile)
    now = datetime.now().strftime("%d.%m %H:%M")
    bot = get_bot()
    old = assign_store.get(feedback_id)
    if old and old.get("assignee_id") != target_uid:
        reassigned_msg = await safe_send_message(
            bot, old["assignee_id"],
            f"❌ Завдання `{feedback_id}` передоручено іншому співробітнику.",
            parse_mode="Markdown")
        if reassigned_msg is not None:
            track_msg(old["assignee_id"], reassigned_msg.message_id)
            if context and getattr(context, "job_queue", None):
                schedule_delete(context, bot, old["assignee_id"], reassigned_msg.message_id, delay=AUTO_DELETE_DELAY)
    assign_store[feedback_id] = {
        "assignee_id": target_uid, "assignee_name": target_name,
        "assigned_by": assigner_name, "assigned_at": now,
    }
    save_assign_store(assign_store)
    assign_line = f"\n👤 Доручено: {md(target_name)} — {md(assigner_name)} {now}"
    _, stored_text = get_msg_data(feedback_id)
    base_text = stored_text if stored_text else (fallback_text or _fallback_task_text(feedback_id))
    lines = [l for l in base_text.split("\n") if not l.startswith("👤 Доручено:")]
    new_text = "\n".join(lines) + assign_line
    new_keyboard = build_task_keyboard(safe_id, assigned=True)
    await edit_all_messages(bot, feedback_id, new_text, new_keyboard)
    # окреме повідомлення виконавцю (без статусів/доручення в тексті)
    _, stored2 = get_msg_data(feedback_id)
    base2 = stored2 if stored2 else (fallback_text or _fallback_task_text(feedback_id))
    task_lines = [l for l in base2.split("\n") if not l.startswith(("✅", "🔄", "↩️", "👤 Доручено:"))]
    task_text = "\n".join(task_lines).strip()
    assignee_keyboard = build_task_keyboard(safe_id)
    msg = await safe_send_message(
        bot, target_uid,
        f"📋 *Нове завдання — `{feedback_id}`*\n{task_text}\n\n_Доручив: {md(assigner_name)} {now}_",
        reply_markup=assignee_keyboard, parse_mode="Markdown")
    if msg is not None:
        track_msg(target_uid, msg.message_id)
        if context and getattr(context, "job_queue", None):
            schedule_delete(context, bot, target_uid, msg.message_id, delay=TASK_DELETE_DELAY)
        if feedback_id in msg_store and isinstance(msg_store[feedback_id], dict) and "ids" in msg_store[feedback_id]:
            msg_store[feedback_id]["ids"][str(target_uid)] = msg.message_id
        else:
            msg_store[feedback_id] = {"ids": {str(target_uid): msg.message_id}, "text": task_text, "photo": None, "photo_path": ""}
        storage.kv_put("msgstore", feedback_id, msg_store[feedback_id])
    add_task_log(feedback_id, f"👤 Доручено {target_name} — {assigner_name} {now}")
    return {"ok": True, "target_name": target_name}


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

    # Видалення (лише власник) — окрема корутина (прибирає живі повідомлення).
    if action == "del":
        res = await task_action_delete(feedback_id, user_id)
        if not res.get("ok"):
            await safe_answer(query, "Тільки власник може видаляти.", show_alert=True)
        return

    # done / wip / cancel — спільне ядро (та сама логіка, що й у дашборді).
    res = await task_action_status(feedback_id, action, user_id,
                                   fallback_text=(query.message.text if query.message else None))
    if not res.get("ok"):
        err = res.get("error")
        if err == "already_closed":
            await safe_answer(query, f"Задачу вже закрито ({res.get('status','')}).", show_alert=True)
        elif err == "no_perm":
            await safe_answer(query, "Немає прав (інший заклад або немає доступу).", show_alert=True)
        else:
            await safe_answer(query, "Невірна дія.", show_alert=True)


# ─── ДОРУЧЕННЯ (ASSIGN) ───────────────────────────────────────────────────────

async def handle_assign(update, context):
    """Показує список співробітників для доручення."""
    query = update.callback_query
    await safe_answer(query)
    user_id = update.effective_user.id

    safe_id = query.data.replace("assign_", "")
    feedback_id = safe_id.replace("_", "-", 2)
    rec = tasks_store.get(feedback_id, {})
    if not can_act_on_task(user_id, rec.get("restaurant")):
        await safe_answer(query, "Немає прав (інший заклад).", show_alert=True)
        return
    # скасувати незавершену автовідправку (без знищення чернетки pending_message)
    cancel_user_jobs(context, user_id, "autosend")
    cancel_user_jobs(context, user_id, "optdelete")
    for k in ('waiting_for_option', 'option_message_id', 'option_chat_id'):
        context.user_data.pop(k, None)

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
    bot = get_bot()
    # Видаляємо через 30 сек
    schedule_delete(context, bot, query.message.chat_id, query.message.message_id, delay=30)

async def handle_assign_to(update, context):
    """Призначає задачу конкретному співробітнику."""
    query = update.callback_query
    await safe_answer(query)

    data = query.data.replace("assignto_", "")
    parts = data.rsplit("_", 1)
    safe_id = parts[0]
    target_uid = int(parts[1])
    feedback_id = safe_id.replace("_", "-", 2)

    res = await task_action_assign(feedback_id, target_uid, update.effective_user.id,
                                   context=context,
                                   fallback_text=(query.message.text if query.message else None))
    if not res.get("ok"):
        if res.get("error") == "no_perm":
            await safe_answer(query, "Немає прав (інший заклад).", show_alert=True)
        else:
            await safe_answer(query, "Не вдалося доручити (неактивний співробітник?).", show_alert=True)
        return

    bot = get_bot()
    await query.edit_message_text(
        f"✅ Завдання `{feedback_id}` доручено *{md(res['target_name'])}*", parse_mode="Markdown")
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

    # Право на коментар: дія в межах закладу або активний виконавець (раніше перевірки не було)
    rec = tasks_store.get(feedback_id, {})
    assigned_info = assign_store.get(feedback_id, {})
    is_assignee = (assigned_info.get("assignee_id") == user_id
                   and user_profiles.get(user_id, {}).get("status") == STATUS_ACTIVE)
    if not can_act_on_task(user_id, rec.get("restaurant")) and not is_assignee:
        await safe_answer(query, "Немає прав.", show_alert=True)
        return

    # Скасовуємо незавершену автовідправку (щоб не створила випадкову задачу), але
    # НЕ знищуємо чернетку (pending_message): лише знімаємо прапорець автовідправки.
    cancel_user_jobs(context, user_id, "autosend")
    cancel_user_jobs(context, user_id, "optdelete")
    for k in ('waiting_for_option', 'option_message_id', 'option_chat_id'):
        context.user_data.pop(k, None)

    cancel_user_jobs(context, user_id, "commentclear")
    # не змішуємо з режимом підсумку зміни
    cancel_user_jobs(context, user_id, "reportclear")
    for k in ('report_mode', 'report_anonymous', 'report_prompt_msg_id', 'report_prompt_chat_id'):
        context.user_data.pop(k, None)

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
        bot = get_bot()
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
    bot = get_bot()
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

    # Сама мутація — спільне ядро (та сама логіка, що й у дашборді); перевірка прав усередині.
    res = await task_action_comment(feedback_id, user_id, update.message.text or "")
    bot = get_bot()
    if res.get("ok"):
        out = f"✅ Коментар до `{feedback_id}` збережено."
    elif res.get("error") == "no_perm":
        out = "❌ Немає прав на коментар."
    else:
        out = "❌ Порожній коментар."

    # Редагуємо промпт замість нового повідомлення
    if prompt_msg_id and prompt_chat_id:
        try:
            await bot.edit_message_text(
                chat_id=prompt_chat_id, message_id=prompt_msg_id, text=out, parse_mode="Markdown")
            # Видаляємо підтвердження через 60 сек
            schedule_delete(context, bot, prompt_chat_id, prompt_msg_id, delay=60)
            return True
        except Exception:
            pass
    sent = await update.message.reply_text(out, parse_mode="Markdown")
    schedule_delete(context, bot, sent.chat_id, sent.message_id, delay=60)
    return True


# ─── ПІДСУМКИ ЗМІН (рефлексивний відгук персоналу) ───────────────────────────
# Окремо від задач: своя KV-таблиця shiftreports, ніяких статусів/кнопок задач,
# ніколи не з'являються в переліку невиконаних. Менеджери бачать ЛИШЕ агреговану
# Claude-сводку (раз/день у дайджесті + on-demand), а не пінг на кожен звіт.

def _should_shift_nudge(user_id):
    """Чи показувати м'який нудж: раз на день, лише активним. Не змінює стан (мітимо ПІСЛЯ відправки)."""
    if not user_id:
        return False
    if _shift_nudged.get(user_id) == datetime.now().strftime("%Y-%m-%d"):
        return False
    return user_profiles.get(user_id, {}).get("status") == STATUS_ACTIVE

def _shift_nudge_line():
    return f"\n\n📝 Наприкінці зміни можна лишити підсумок — кнопка «{SHIFT_REPORT_BTN}» внизу."

def _mark_shift_nudged(user_id):
    if user_id:
        _shift_nudged[user_id] = datetime.now().strftime("%Y-%m-%d")

async def enter_shift_report_mode(update, context):
    """Вмикає режим підсумку зміни: наступний текст/голос буде збережено як звіт."""
    user_id = update.effective_user.id
    prof = user_profiles.get(user_id)
    if not prof or prof.get("status") != STATUS_ACTIVE:
        await update.message.reply_text("Спочатку зареєструйтесь і дочекайтесь підтвердження: /start")
        return
    # Чистий перехід у режим підсумку: скасовуємо незавершену автовідправку (інакше за ~20с
    # вона створила б випадкову задачу) і виходимо з інших режимів (коментар/редагування промпту),
    # щоб не було конфлікту маршрутизації тексту/голосу.
    cancel_user_jobs(context, user_id, "autosend")
    cancel_user_jobs(context, user_id, "optdelete")
    cancel_user_jobs(context, user_id, "photoclear")
    cancel_user_jobs(context, user_id, "commentclear")
    cancel_user_jobs(context, user_id, "promptedit")
    cancel_user_jobs(context, user_id, "reportclear")
    for k in ('waiting_for_option', 'pending_message', 'message_type', 'waiting_for_photo',
              'pending_photo', 'option_message_id', 'option_chat_id',
              'commenting_on', 'comment_safe_id', 'comment_prompt_msg_id', 'comment_prompt_chat_id',
              'prompt_editing', 'prompt_working', 'prompt_original', 'prompt_msg_id', 'prompt_chat_id'):
        context.user_data.pop(k, None)
    context.user_data['report_mode'] = True
    context.user_data['report_anonymous'] = False
    bot = get_bot()
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🕵️ Анонімно", callback_data="report_anon"),
        InlineKeyboardButton("❌ Скасувати", callback_data="report_cancel"),
    ]])
    sent = await safe_send_message(
        bot, update.effective_chat.id,
        "🌙 *Підсумок зміни.* Як пройшла зміна? Що добре, що покращити, побажання?\n"
        "Напишіть або надиктуйте одним повідомленням 👇\n_(необов'язково; автоскасується за 2 хв)_",
        reply_markup=keyboard, parse_mode="Markdown")
    if sent:
        track_msg(sent.chat_id, sent.message_id)
        context.user_data['report_prompt_msg_id'] = sent.message_id
        context.user_data['report_prompt_chat_id'] = sent.chat_id
    context.job_queue.run_once(
        report_clear_callback, when=120, name=f"reportclear_{user_id}",
        data={"user_id": user_id, "chat_id": update.effective_chat.id})

async def report_clear_callback(context):
    """Скидає режим підсумку зміни через 2 хв (клон comment_clear_callback)."""
    user_id = context.job.data["user_id"]
    user_data = context.application.user_data.get(user_id, {})
    if user_data.get('report_mode'):
        user_data.pop('report_mode', None)
        user_data.pop('report_anonymous', None)
        pmid = user_data.pop('report_prompt_msg_id', None)
        pcid = user_data.pop('report_prompt_chat_id', None)
        bot = get_bot()
        if pmid and pcid:
            try:
                await bot.edit_message_text(chat_id=pcid, message_id=pmid,
                    text="⏱ Час вийшов. Режим підсумку зміни скасовано.")
                schedule_delete(context, bot, pcid, pmid, delay=30)
            except Exception:
                pass

async def handle_report_cancel(update, context):
    """Скасування режиму підсумку зміни кнопкою."""
    query = update.callback_query
    await safe_answer(query)
    user_id = update.effective_user.id
    cancel_user_jobs(context, user_id, "reportclear")
    for k in ('report_mode', 'report_anonymous', 'report_prompt_msg_id', 'report_prompt_chat_id'):
        context.user_data.pop(k, None)
    try:
        await query.edit_message_text("❌ Підсумок зміни скасовано.")
    except Exception:
        pass
    schedule_delete(context, get_bot(), query.message.chat_id, query.message.message_id, delay=30)

async def handle_report_anon(update, context):
    """Перемикач анонімності в режимі підсумку зміни."""
    query = update.callback_query
    if not context.user_data.get('report_mode'):
        await safe_answer(query, "Режим підсумку вже завершено.", show_alert=True)
        return
    anon = not context.user_data.get('report_anonymous', False)
    context.user_data['report_anonymous'] = anon
    await safe_answer(query, "🕵️ Анонімно увімкнено" if anon else "👤 Анонімність вимкнено")
    label = "✅ 🕵️ Анонімно" if anon else "🕵️ Анонімно"
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(label, callback_data="report_anon"),
        InlineKeyboardButton("❌ Скасувати", callback_data="report_cancel"),
    ]])
    try:
        await query.edit_message_reply_markup(reply_markup=keyboard)
    except Exception:
        pass

def save_shift_report(user_id, profile, text, source, anonymous):
    """Upsert звіту за (user, день). Повторно того ж дня — ДОПИСУЄ. Повертає True якщо дописано.
    Анонімність застосовується до всього рядка (ім'я/роль прибираються), заклад лишається."""
    day = datetime.now().strftime("%Y-%m-%d")
    key = f"{user_id}:{day}"
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    existing = reports_store.get(key)
    appended = False
    if isinstance(existing, dict) and existing.get("text"):
        existing["text"] = existing["text"].rstrip() + "\n— — —\n" + text
        existing["updated_at"] = now
        if anonymous:
            existing["is_anonymous"] = True
            existing["sender_name"] = None
            existing["sender_role"] = None
        rec = existing
        appended = True
    else:
        rec = {
            "telegram_id": user_id, "business_day": day,
            "created_at": now, "updated_at": now,
            "sender_name": None if anonymous else get_display_name(profile),
            "sender_role": None if anonymous else profile.get("role", "—"),
            "restaurant": profile.get("restaurant", "—"),
            "is_anonymous": bool(anonymous), "text": text, "source": source,
        }
    reports_store[key] = rec
    storage.kv_put("shiftreports", key, rec)
    _shift_summary_cache.pop(day, None)          # інвалідовуємо кеш сводки за сьогодні
    if SHIFT_REPORTS_TO_SHEET:
        asyncio.create_task(_mirror_report_to_sheet(rec))
    return appended

async def process_shift_report(update, context):
    """Зберігає підсумок зміни (текст або голос); викликається у report_mode."""
    user_id = update.effective_user.id
    bot = get_bot()
    profile = user_profiles.get(user_id, {})
    if update.message.voice:
        try:
            vf = await update.message.voice.get_file()
            fp = os.path.join(tempfile.gettempdir(), f"shift_{user_id}_{uuid.uuid4().hex[:8]}.ogg")
            await vf.download_to_drive(fp)
            text = await transcribe_voice(fp)     # видаляє файл у finally
        except Exception as e:
            logger.error(f"Shift report voice error: {e}")
            text = None
        source = "voice"
    else:
        text = update.message.text or ""
        source = "text"

    anonymous = bool(context.user_data.get('report_anonymous'))
    cancel_user_jobs(context, user_id, "reportclear")
    context.user_data.pop('report_mode', None)
    context.user_data.pop('report_anonymous', None)
    pmid = context.user_data.pop('report_prompt_msg_id', None)
    pcid = context.user_data.pop('report_prompt_chat_id', None)

    if not text or not text.strip():
        out = "❌ Не вдалося розпізнати підсумок. Натисніть кнопку ще раз."
    else:
        appended = save_shift_report(user_id, profile, text.strip(), source, anonymous)
        out = "✅ Підсумок зміни оновлено 🙏" if appended else "✅ Дякуємо! Підсумок зміни збережено 🙏"

    # Редагуємо промпт (інакше — нове повідомлення); БЕЗ reply-клавіатури, з автовидаленням.
    if pmid and pcid:
        try:
            await bot.edit_message_text(chat_id=pcid, message_id=pmid, text=out)
            schedule_delete(context, bot, pcid, pmid, delay=AUTO_DELETE_DELAY)
            return
        except Exception:
            pass
    sent = await safe_send_message(bot, update.effective_chat.id, out, parse_mode=None)
    if sent:
        track_msg(sent.chat_id, sent.message_id)
        schedule_delete(context, bot, sent.chat_id, sent.message_id, delay=AUTO_DELETE_DELAY)

def _shift_reports_for_day(day):
    """Список звітів (dict) за вказаний business_day."""
    return [v for v in reports_store.values()
            if isinstance(v, dict) and v.get("business_day") == day]

def _fallback_shift_summary(reports):
    """Простий список, якщо Claude недоступний (plain text, без markdown)."""
    parts = []
    for r in reports:
        who = "анонім" if r.get("is_anonymous") else (r.get("sender_name") or "—")
        t = (r.get("text", "") or "")[:300]
        parts.append(f"• [{r.get('restaurant', '—')}] {who}: {t}")
    return "\n".join(parts) if parts else "—"

def _summarize_shift_reports_sync(reports):
    """Впорядковує звіти для керівника, МАКСИМАЛЬНО зберігаючи формулювання — ОДИН виклик Claude.
    Не стискає заради стислості й нічого не додумує (краще дослівно, ніж перекрутити)."""
    lines = []
    for r in reports:
        who = "анонім" if r.get("is_anonymous") else (r.get("sender_name") or "—")
        lines.append(f"[{r.get('restaurant', '—')}] {who}: {r.get('text', '')}")
    prompt = (
        "Ось підсумки змін персоналу ресторанів за день (по одному на рядок, у дужках — заклад).\n"
        "Впорядкуй їх для керівника, МАКСИМАЛЬНО ЗБЕРІГАЮЧИ зміст і формулювання працівників. Правила:\n"
        "1) Прибирай ЛИШЕ зайві/порожні слова (вода, повтори, «ну», «короче», вигуки) — суть НЕ скорочуй.\n"
        "2) За замовчуванням давай ПОВНО: краще довше, ніж втратити деталь, цифру чи нюанс. "
        "Не стискай заради стислості.\n"
        "3) НЕ перефразовуй на свій лад і НЕ перекручуй сказане. Якщо не впевнений, як сформулювати "
        "коротше — наведи ДОСЛІВНО, як сказав працівник.\n"
        "4) НІЧОГО не додумуй і не додавай від себе — лише те, що реально є в тексті.\n"
        "5) Зберігай конкретику: імена, цифри, назви обладнання, столики, час тощо.\n"
        "6) Згрупуй ПО ЗАКЛАДАХ. Якщо кілька людей сказали те саме — об'єднай і познач (×N), "
        "але унікальні деталі окремих людей не втрачай.\n"
        "Формат: заголовок '🍽 <Заклад>', далі марковані пункти '• ...'. Анонімні — без імені.\n\n"
        + "\n".join(lines)
    )
    resp = _call_claude_sync(prompt, max_tokens=2000)
    return resp.content[0].text.strip()

async def _render_shift_summary(day, reports):
    """Текст сводки за день; кеш за (day,count); фолбек — простий список."""
    count = len(reports)
    cached = _shift_summary_cache.get(day)
    if cached and cached[0] == count:
        return cached[1]
    try:
        text = await _arun(lambda: _retry_sync(lambda: _summarize_shift_reports_sync(reports)))
    except Exception as e:
        logger.error(f"Shift summary Claude failed: {e}")
        text = _fallback_shift_summary(reports)
    _shift_summary_cache[day] = (count, text)
    return text

async def _send_summary_block(bot, uid, body, with_keyboard=False):
    """Шле зведення; якщо довше за ліміт Telegram (~4096) — ріже по рядках на частини, щоб
    повніший текст не загубився. Кожну частину трекаємо; reply-клавіатуру (за потреби) чіпляємо
    лише до ПЕРШОЇ частини (вона постійна, не автовидаляється). Повертає перше повідомлення."""
    LIMIT = 3900
    chunks, cur = [], ""
    for line in body.split("\n"):
        while len(line) > LIMIT:                       # один наддовгий рядок теж ріжемо
            if cur:
                chunks.append(cur); cur = ""
            chunks.append(line[:LIMIT]); line = line[LIMIT:]
        if cur and len(cur) + 1 + len(line) > LIMIT:
            chunks.append(cur); cur = ""
        cur = (cur + "\n" + line) if cur else line
    if cur:
        chunks.append(cur)
    first = None
    for i, ch in enumerate(chunks):
        rm = get_main_keyboard(uid) if (with_keyboard and i == 0) else None
        m = await safe_send_message(bot, uid, ch, reply_markup=rm, parse_mode=None)
        if m:
            track_msg(uid, m.message_id)
            if first is None:
                first = m
    return first

async def send_shift_reports_section(bot, recipients, context):
    """Додає у дайджест ОДИН агрегований блок підсумків змін за вчора. Тихий день — нічого."""
    yday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    reports = _shift_reports_for_day(yday)
    if not reports:
        return
    ymd = (datetime.now() - timedelta(days=1)).strftime("%d.%m")
    summary = await _render_shift_summary(yday, reports)
    body = f"📝 Підсумки змін за {ymd} ({len(reports)})\n\n{summary}"
    for uid in recipients:
        await _send_summary_block(bot, uid, body)

def _purge_old_shift_reports():
    """Чистить звіти старші за SHIFT_REPORTS_RETENTION_DAYS (рядки YYYY-MM-DD порівнюються лексикографічно)."""
    cutoff = (datetime.now() - timedelta(days=SHIFT_REPORTS_RETENTION_DAYS)).strftime("%Y-%m-%d")
    old = [k for k, v in list(reports_store.items())
           if isinstance(v, dict) and str(v.get("business_day", "")) < cutoff]
    for k in old:
        reports_store.pop(k, None)
        storage.kv_delete("shiftreports", k)
    if old:
        logger.info(f"Purged {len(old)} old shift reports (< {cutoff})")

async def show_shift_reports(query, context):
    """On-demand для менеджерів: Claude-сводка + окремі звіти з кнопкою ескалації в задачу."""
    uid = query.from_user.id
    bot = get_bot()
    today = datetime.now().strftime("%Y-%m-%d")
    yday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    day = today if _shift_reports_for_day(today) else yday
    reports = _shift_reports_for_day(day)
    if not reports:
        await query.edit_message_text("📝 Підсумків змін поки немає.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="menu_back")]]))
        return
    await delete_tracked_messages(bot, [uid])
    ymd = datetime.strptime(day, "%Y-%m-%d").strftime("%d.%m")
    summary = await _render_shift_summary(day, reports)
    body = f"📝 Підсумки змін за {ymd} ({len(reports)})\n\n{summary}"
    await _send_summary_block(bot, uid, body, with_keyboard=True)
    for r in reports:
        key = f"{r.get('telegram_id')}:{r.get('business_day')}"
        who = "анонім" if r.get("is_anonymous") else (r.get("sender_name") or "—")
        txt = f"🌙 [{r.get('restaurant', '—')}] {who}\n{r.get('text', '')}"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("➕ Створити задачу", callback_data=f"r2task_{key}")]])
        m = await safe_send_message(bot, uid, txt, reply_markup=kb, parse_mode=None)
        if m:
            track_msg(uid, m.message_id)
            schedule_delete(context, bot, uid, m.message_id, delay=TASK_DELETE_DELAY)

async def handle_report_to_task(update, context):
    """Ескалація одного звіту в звичайну задачу (ручна, для can_manage_tasks).
    Права перевіряємо ДО відповіді на callback (його можна відповісти лише раз)."""
    query = update.callback_query
    user_id = update.effective_user.id
    if not can_manage_tasks(user_id):
        await safe_answer(query, "Немає прав.", show_alert=True)
        return
    key = query.data[len("r2task_"):]
    rec = reports_store.get(key)
    if not isinstance(rec, dict) or not rec.get("text"):
        await safe_answer(query, "Звіт не знайдено.", show_alert=True)
        return
    await safe_answer(query, "Створюю задачу…")
    anon = bool(rec.get("is_anonymous"))
    profile = {"restaurant": rec.get("restaurant", "Терраса"), "role": rec.get("sender_role") or "—",
               "telegram_id": (None if anon else rec.get("telegram_id"))}
    user_data = {"sender_name": rec.get("sender_name") or "Підсумок зміни",
                 "sender_role": rec.get("sender_role") or "—", "is_anonymous": anon}
    results = await analyze_with_claude(rec["text"], profile, is_anonymous=anon)
    _, ids, _ = await _save_and_notify(results, user_data, profile, None, context)
    # Підтвердження — редагуванням повідомлення (видиме й стійке), кнопку прибираємо.
    base = query.message.text if query.message else ""
    try:
        await query.edit_message_text(f"{base}\n\n✅ Створено задачу(і): {', '.join(str(i) for i in ids)}")
    except Exception:
        pass

async def _mirror_report_to_sheet(rec):
    """Опц. дзеркало звіту в окрему вкладку Google Sheets (best-effort, off за замовч.)."""
    def _do():
        ws = _get_reports_worksheet()
        c = rec.get("created_at", "")
        ws.append_row([
            rec.get("business_day", ""), c[11:16] if len(c) >= 16 else "",
            rec.get("restaurant", "—"), rec.get("sender_name") or "—",
            rec.get("sender_role") or "—", "так" if rec.get("is_anonymous") else "—",
            rec.get("source", "text"), rec.get("text", ""),
        ])
    try:
        await _arun(lambda: _sheet_retry(_do))
    except Exception as e:
        logger.error(f"Shift report sheet mirror failed: {e}")


# ─── ДАЙДЖЕСТ / ДНИ НАРОДЖЕННЯ ───────────────────────────────────────────────

def _is_resolved(status):
    """Задача вважається закритою лише якщо статус ПОЧИНАЄТЬСЯ з 'Виконано'/'Видалено'
    (а не просто містить ці слова) — щоб коментар/ім'я не сховали відкриту задачу."""
    st = str(status).strip()
    return st.startswith("Виконано") or st.startswith("Видалено")


def _store_task_message(fid, uid, message_id, text, photo=None, photo_path=""):
    """Записує ОДИН актуальний message_id для (задача, отримувач) — без накопичення
    застарілих id. Нормалізує legacy-записи."""
    if fid == "—" or not fid:
        return
    entry = msg_store.get(fid)
    if not (isinstance(entry, dict) and "ids" in entry):
        entry = {"ids": (entry if isinstance(entry, dict) else {}), "text": text, "photo": photo, "photo_path": photo_path}
    entry["ids"][str(uid)] = message_id      # перезапис, не додавання — старі вже видалені
    entry["text"] = text
    if photo is not None:
        entry["photo"] = photo
    if photo_path:
        entry["photo_path"] = photo_path
    msg_store[fid] = entry


def _photo_deliverable(fid):
    """True, якщо для задачі є РЕАЛЬНО доставне фото: збережений Telegram file_id
    або наявний локальний файл. Маркер 📷 ставимо лише тоді — інакше історичні/імпортовані
    з таблиці задачі (has_photo=True, але file_id у msgstore немає) показували б 📷 без фото."""
    entry = msg_store.get(fid)
    if not isinstance(entry, dict):
        return False
    if entry.get("photo"):
        return True
    pp = entry.get("photo_path", "")
    return bool(pp) and os.path.exists(pp)


def _row_age_days(row):
    """Вік задачі в днях (за _ts/created_ts, фолбек — Дата+Час)."""
    ts = row.get("_ts")
    try:
        if ts:
            dt = datetime.strptime(str(ts)[:19], "%Y-%m-%d %H:%M:%S")
        else:
            dt = datetime.strptime(f"{row.get('Дата','')} {row.get('Час','00:00')}", "%d.%m.%Y %H:%M")
        return max(0, (datetime.now() - dt).days)
    except Exception:
        return 0

def build_row_text(row, fid, safe_id):
    """Формує текст і клавіатуру для рядка задачі з таблиці."""
    status = str(row.get("Статус", "Нове"))
    status_icon = "🔴" if status.startswith("Нове") else "🟡"
    category = row.get("Категорія", "—")
    cat_icon = CATEGORY_ICONS.get(category, "📌")
    date = row.get("Дата", "—")
    time_val = row.get("Час", "")
    sender = row.get("Хто повідомив", "—")
    summary = row.get("Суть", "—")
    history = row.get("Лог", "")
    # Маркер 📷 — за реальною доставністю фото (msgstore), а не за tasks.has_photo,
    # щоб не показувати 📷 на імпортованих задачах без збереженого file_id.
    has_photo = _photo_deliverable(fid)
    date_str = f"{date} {time_val}".strip()

    assigned = fid in assign_store
    assign_info = assign_store.get(fid, {})
    photo_mark = " 📷" if has_photo else ""
    urg_icon = URGENCY_ICONS.get(str(row.get("Терміновість", "")), "")
    age = _row_age_days(row)
    extra = (" " + urg_icon if urg_icon else "") + (f" ⏰{age}д" if age >= 2 else "")
    if not assigned and status.startswith("Нове"):
        extra += " ⚠️без власника"

    text = (
        f"{status_icon} `{fid}` · {cat_icon} *{md(category)}*{photo_mark}{extra}\n"
        f"{md(summary)}\n"
        f"_{md(date_str)} · {md(sender)}_"
    )
    if assign_info:
        text += f"\n👤 Доручено: {md(assign_info.get('assignee_name', '—'))} — {md(assign_info.get('assigned_by', '—'))} {assign_info.get('assigned_at', '')}"
    if history and history != "Нове":
        entries = [e.strip() for e in str(history).split("|") if e.strip() and e.strip() != "Нове"]
        if entries:
            text += "\n\n📋 _Історія:_\n" + "\n".join(f"_{md(e)}_" for e in entries)

    keyboard = build_task_keyboard(safe_id, assigned=assigned)
    return text, keyboard

async def send_unresolved_digest(context):
    """Відправляє о 06:00 UTC (09:00 Київ) дайджест:
    1. Видаляє попередні відстежені повідомлення у всіх отримувачів
    2. Повідомлення 1: виконані вчора (без кнопок)
    3. Повідомлення 2+: кожна невиконана задача окремо з кнопками
    """
    try:
        bot = get_bot()
        recipients = list(get_recipients())
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%d.%m.%Y")
        all_rows = [_task_record(r) for _, r in tasks_store.items() if str(r.get("fid", "")).strip()]

        # ── 1. Очищення чату всім отримувачам ─────────────────────────────────
        await delete_tracked_messages(bot, recipients)

        # ── 2.5 Підсумки змін (агреговано) — захищено, щоб збій не зірвав дайджест ──
        try:
            await send_shift_reports_section(bot, recipients, context)
            _purge_old_shift_reports()
        except Exception as e:
            logger.error(f"Digest shift section error: {e}")

        # ── Персонально кожному: лише його заклади, відсортовано за терміновістю+віком ──
        for uid in recipients:
            venues = visible_restaurants(uid)
            mine = all_rows if venues == set(RESTAURANTS) else [r for r in all_rows if r.get("Заклад") in venues]
            unresolved = [r for r in mine if not _is_resolved(r.get("Статус", ""))]
            completed = [r for r in mine if str(r.get("Статус", "")).strip().startswith("Виконано")
                         and r.get("_completed") == yesterday]
            multi_venue = (venues == set(RESTAURANTS)) or len(venues) > 1
            # Сортуємо: при кількох закладах — спершу за закладом (для груп. заголовків), тоді терміновість+вік
            if multi_venue:
                unresolved.sort(key=lambda r: (r.get("Заклад", ""), _urg_rank(r.get("Терміновість", "")), -_row_age_days(r)))
            else:
                unresolved.sort(key=lambda r: (_urg_rank(r.get("Терміновість", "")), -_row_age_days(r)))

            # Виконано вчора
            if completed:
                dt = f"✅ *Виконано вчора ({yesterday}): {len(completed)}*\n\n"
                for row in completed:
                    ci = CATEGORY_ICONS.get(row.get("Категорія", "—"), "📌")
                    dt += f"✅ `{row.get('Номер','—')}` · {ci} {md(row.get('Суть','—'))}\n"
                m = await safe_send_message(bot, uid, dt.strip(), reply_markup=get_main_keyboard(uid), parse_mode="Markdown")
            else:
                m = await safe_send_message(bot, uid, f"✅ *Вчора ({yesterday}) виконаних завдань немає.*",
                                            reply_markup=get_main_keyboard(uid), parse_mode="Markdown")
            if m:
                track_msg(uid, m.message_id)

            if not unresolved:
                m = await safe_send_message(bot, uid, "🎉 *Всі завдання виконано!*",
                                            reply_markup=get_main_keyboard(uid), parse_mode="Markdown")
                if m:
                    track_msg(uid, m.message_id)
                continue

            m = await safe_send_message(bot, uid, f"🔴 *Невиконані завдання: {len(unresolved)}*", parse_mode="Markdown")
            if m:
                track_msg(uid, m.message_id)

            cur_venue = None
            for row in unresolved:
                try:
                    fid = row.get("Номер", "—")
                    safe_id = fid.replace("-", "_")
                    if multi_venue and row.get("Заклад") != cur_venue:
                        cur_venue = row.get("Заклад")
                        hv = await safe_send_message(bot, uid, f"🏠 *{md(cur_venue or '—')}*", parse_mode="Markdown")
                        if hv:
                            track_msg(uid, hv.message_id)
                    _, keyboard = build_row_text(row, fid, safe_id)
                    raw = msg_store.get(fid)
                    stored_text = raw.get("text", "") if isinstance(raw, dict) else ""
                    stored_photo = raw.get("photo") if isinstance(raw, dict) else None
                    stored_photo_path = raw.get("photo_path", "") if isinstance(raw, dict) else ""
                    text = stored_text if stored_text else build_row_text(row, fid, safe_id)[0]
                    msg = await safe_send_message(bot, uid, text, reply_markup=keyboard, parse_mode="Markdown")
                    if msg is None:
                        continue
                    track_msg(uid, msg.message_id)
                    schedule_delete(context, bot, uid, msg.message_id, delay=TASK_DELETE_DELAY)
                    if stored_photo:
                        pm = await safe_send_photo(bot, uid, file_id=stored_photo, file_path=stored_photo_path)
                        if pm is not None:
                            track_msg(uid, pm.message_id)
                            schedule_delete(context, bot, uid, pm.message_id, delay=TASK_DELETE_DELAY)
                    _store_task_message(fid, uid, msg.message_id, text, photo=stored_photo, photo_path=stored_photo_path)
                    if fid != "—":
                        storage.kv_put("msgstore", fid, msg_store[fid])
                    await asyncio.sleep(0.03)
                except Exception as e:
                    logger.error(f"Digest item {row.get('Номер')} for {uid}: {e}")

        storage.meta_set("last_digest", datetime.now().strftime("%Y-%m-%d"))
    except Exception as e:
        logger.error(f"Unresolved digest error: {e}")

async def send_birthday_greetings(context):
    try:
        bot = get_bot()
        today = datetime.now().strftime("%d.%m")
        birthday_users = [(uid, p) for uid, p in user_profiles.items()
                          if p.get("status") == STATUS_ACTIVE and p.get("birthday", "")[:5] == today]
        for uid, profile in birthday_users:
            await safe_send_message(bot, uid, f"🎂 З Днем народження, {profile.get('first_name', '')}! 🎉\nВітаємо від усієї команди! Бажаємо здоров'я та успіхів! 🥳", parse_mode=None)
        if birthday_users:
            names = ", ".join(md(get_display_name(p)) for _, p in birthday_users)
            for admin_id in get_recipients():
                msg = await safe_send_message(bot, admin_id, f"🎂 Сьогодні день народження у: *{names}*", parse_mode="Markdown")
                if msg:
                    track_msg(admin_id, msg.message_id)
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
        max_tokens=4000,   # промпт ~1300 токенів; із запасом, щоб оновлена версія не обрізалась
        messages=[{"role": "user", "content": meta_prompt}]
    )
    return response.content[0].text.strip()

async def _apply_prompt_instruction(context, user_id, instruction):
    """Спільне ядро редагування промпту: інструкція (з голосу АБО тексту) → новий варіант."""
    bot = get_bot()
    cancel_user_jobs(context, user_id, "promptedit")
    prompt_msg_id = context.user_data.get('prompt_msg_id')
    prompt_chat_id = context.user_data.get('prompt_chat_id')
    try:
        await bot.edit_message_text(chat_id=prompt_chat_id, message_id=prompt_msg_id,
                                    text="⏳ Генерую новий промпт...", reply_markup=None)
    except Exception:
        pass
    try:
        current = context.user_data.get('prompt_working', load_prompt())
        new_prompt = await _arun(lambda: refine_prompt_with_claude(current, instruction))
    except Exception as e:
        logger.error(f"Prompt refinement error: {e}")
        new_prompt = None

    if not new_prompt:
        try:
            await bot.edit_message_text(chat_id=prompt_chat_id, message_id=prompt_msg_id,
                text="❌ Помилка генерації. Спробуйте ще раз (голосом або текстом).",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Скасувати", callback_data="menu_cancel_edit_prompt")]]))
        except Exception:
            pass
        context.job_queue.run_once(prompt_edit_timeout_callback, when=300,
            name=f"promptedit_{user_id}", data={"user_id": user_id})
        return

    context.user_data['prompt_working'] = new_prompt
    display = new_prompt if len(new_prompt) <= 3000 else new_prompt[:3000] + "\n...(скорочено)"
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Прийняти", callback_data="menu_accept_prompt")],
        [InlineKeyboardButton("✏️ Редагувати далі", callback_data="menu_edit_prompt")],
        [InlineKeyboardButton("❌ Скасувати", callback_data="menu_cancel_edit_prompt")],
    ])
    try:
        await bot.edit_message_text(chat_id=prompt_chat_id, message_id=prompt_msg_id,
            text=f"📋 *Новий варіант промпту:*\n\n`{display}`", parse_mode="Markdown", reply_markup=keyboard)
    except Exception as e:
        logger.error(f"Edit prompt msg error: {e}")


async def process_prompt_text(update, context):
    """Редагування промпту ТЕКСТОМ (інструкція = надісланий текст)."""
    user_id = update.effective_user.id
    instruction = (update.message.text or "").strip()
    if not instruction:
        return
    await _apply_prompt_instruction(context, user_id, instruction)


async def process_prompt_voice(update, context):
    """Редагування промпту ГОЛОСОМ: транскрибуємо й застосовуємо ту саму логіку."""
    user_id = update.effective_user.id
    bot = get_bot()
    cancel_user_jobs(context, user_id, "promptedit")
    prompt_msg_id = context.user_data.get('prompt_msg_id')
    prompt_chat_id = context.user_data.get('prompt_chat_id')
    try:
        await bot.edit_message_text(chat_id=prompt_chat_id, message_id=prompt_msg_id,
                                    text="⏳ Розпізнаю голосове...", reply_markup=None)
    except Exception:
        pass
    voice_file = await update.message.voice.get_file()
    file_path = os.path.join(tempfile.gettempdir(), f"promptvoice_{user_id}.ogg")
    await voice_file.download_to_drive(file_path)
    instruction = await transcribe_voice(file_path)
    if not instruction:
        working = context.user_data.get('prompt_working', load_prompt())
        display = working if len(working) <= 3000 else working[:3000] + "\n...(скорочено)"
        try:
            await bot.edit_message_text(chat_id=prompt_chat_id, message_id=prompt_msg_id,
                text=f"❌ Не вдалося розпізнати. Спробуйте ще раз (голосом або текстом).\n\n`{display}`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Скасувати", callback_data="menu_cancel_edit_prompt")]]))
        except Exception:
            pass
        context.job_queue.run_once(prompt_edit_timeout_callback, when=300,
            name=f"promptedit_{user_id}", data={"user_id": user_id})
        return
    await _apply_prompt_instruction(context, user_id, instruction)

# ─── АДМІН-МЕНЮ ──────────────────────────────────────────────────────────────

def build_admin_menu_keyboard(user_id):
    """Меню залежно від ролі. «Адміністратор» (can_manage_tasks, але НЕ is_admin) —
    лише «Невиконані завдання»; повні адміни (Управляючий/Виконавчий директор/власник) —
    увесь набір; власник — додатково керування промптом."""
    keyboard = [[InlineKeyboardButton("🔴 Невиконані завдання", callback_data="menu_unresolved")],
                [InlineKeyboardButton("📝 Підсумки змін", callback_data="menu_shiftreports")]]
    if is_admin(user_id):
        keyboard += [
            [InlineKeyboardButton("👥 Список співробітників", callback_data="menu_staff")],
            [InlineKeyboardButton("⏳ Очікують підтвердження", callback_data="menu_pending")],
            [InlineKeyboardButton("🔄 Змінити роль", callback_data="menu_changerole")],
            [InlineKeyboardButton("🏠 Доступ до закладів", callback_data="menu_venues")],
            [InlineKeyboardButton("❌ Звільнити", callback_data="menu_fire")],
            [InlineKeyboardButton("♻️ Відновити співробітника", callback_data="menu_restore")],
        ]
    if is_owner(user_id):
        keyboard += [
            [InlineKeyboardButton("📋 Поточний промпт", callback_data="menu_view_prompt")],
            [InlineKeyboardButton("✏️ Змінити промпт", callback_data="menu_edit_prompt")],
        ]
    return InlineKeyboardMarkup(keyboard)


# ─── ДОСТУП ДО ЗАКЛАДІВ (per-користувач: один / кілька / усі) ─────────────────

def _venue_access_text(uid):
    p = user_profiles.get(uid, {})
    venues = _profile_venues(p)
    cur = "усі заклади" if venues == set(RESTAURANTS) else ", ".join(r for r in RESTAURANTS if r in venues)
    return (f"🏠 *Доступ: {md(get_display_name(p))}* ({md(p.get('role','—'))})\n"
            f"Зараз бачить: *{md(cur)}*\n\nНатискайте, щоб увімкнути/вимкнути заклад:")

def _venue_access_keyboard(uid):
    venues = _profile_venues(user_profiles.get(uid, {}))
    all_sel = venues == set(RESTAURANTS)
    rows = [[InlineKeyboardButton(f"{'✅' if r in venues else '▫️'} {r}", callback_data=f"vtoggle_{uid}_{i}")]
            for i, r in enumerate(RESTAURANTS)]
    rows.append([InlineKeyboardButton(("✅ " if all_sel else "") + "🌐 Усі заклади", callback_data=f"vall_{uid}")])
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="menu_venues")])
    return InlineKeyboardMarkup(rows)

async def handle_venue_user(update, context):
    query = update.callback_query
    await safe_answer(query)
    if not is_admin(update.effective_user.id):
        await safe_answer(query, "Немає прав.", show_alert=True)
        return
    uid = int(query.data.replace("venueuser_", ""))
    if uid not in user_profiles:
        await safe_answer(query, "Користувача не знайдено.", show_alert=True)
        return
    await query.edit_message_text(_venue_access_text(uid), reply_markup=_venue_access_keyboard(uid), parse_mode="Markdown")

async def handle_venue_toggle(update, context):
    query = update.callback_query
    await safe_answer(query)
    if not is_admin(update.effective_user.id):
        await safe_answer(query, "Немає прав.", show_alert=True)
        return
    uid_str, idx = query.data.replace("vtoggle_", "").rsplit("_", 1)
    uid = int(uid_str)
    p = user_profiles.get(uid)
    if not p or not idx.isdigit() or int(idx) >= len(RESTAURANTS):
        await safe_answer(query, "Помилка.", show_alert=True)
        return
    r = RESTAURANTS[int(idx)]
    cur = set(_profile_venues(p))   # старт із поточного видимого набору
    cur.discard(r) if r in cur else cur.add(r)
    if not cur:
        await safe_answer(query, "Має лишитись хоча б один заклад.", show_alert=True)
        return
    p["manages"] = [v for v in RESTAURANTS if v in cur]
    save_profiles(user_profiles)
    await query.edit_message_text(_venue_access_text(uid), reply_markup=_venue_access_keyboard(uid), parse_mode="Markdown")

async def handle_venue_all(update, context):
    query = update.callback_query
    await safe_answer(query)
    if not is_admin(update.effective_user.id):
        await safe_answer(query, "Немає прав.", show_alert=True)
        return
    uid = int(query.data.replace("vall_", ""))
    p = user_profiles.get(uid)
    if not p:
        await safe_answer(query, "Користувача не знайдено.", show_alert=True)
        return
    p["manages"] = list(RESTAURANTS)
    save_profiles(user_profiles)
    await query.edit_message_text(_venue_access_text(uid), reply_markup=_venue_access_keyboard(uid), parse_mode="Markdown")


async def show_admin_menu(update, context):
    user_id = update.effective_user.id
    if not can_manage_tasks(user_id):
        await update.message.reply_text("❌ Немає прав.")
        return
    msg = await update.message.reply_text(
        "👨‍💼 *Адмін-меню*\nОберіть дію:",
        reply_markup=build_admin_menu_keyboard(user_id),
        parse_mode="Markdown"
    )
    track_msg(msg.chat_id, msg.message_id)
    _bot = get_bot()
    schedule_delete(context, _bot, msg.chat_id, msg.message_id, delay=MENU_DELETE_DELAY)

async def handle_admin_menu(update, context):
    query = update.callback_query
    await safe_answer(query)
    user_id = update.effective_user.id

    if not can_manage_tasks(user_id):
        await safe_answer(query, "Немає прав.", show_alert=True)
        return

    action = query.data.replace("menu_", "")

    # «Адміністратор» (can_manage_tasks, але не is_admin) має лише «Невиконані» та повернення;
    # керування персоналом — тільки повним адмінам, керування промптом — лише власнику (нижче).
    if action in {"staff", "pending", "changerole", "fire", "restore", "venues"} and not is_admin(user_id):
        await safe_answer(query, "Немає прав.", show_alert=True)
        return

    if action == "unresolved":
        await show_unresolved(query, context)
        return

    if action == "shiftreports":
        await show_shift_reports(query, context)
        return

    if action == "venues":
        active = [(uid, p) for uid, p in user_profiles.items()
                  if p.get("status") == STATUS_ACTIVE and p.get("role") in RECIPIENT_ROLES]
        if not active:
            await query.edit_message_text("Немає керівників для налаштування доступу.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="menu_back")]]))
            return
        kb = [[InlineKeyboardButton(f"{get_display_name(p)} ({p.get('role','—')})",
                                    callback_data=f"venueuser_{uid}")] for uid, p in active]
        kb.append([InlineKeyboardButton("◀️ Назад", callback_data="menu_back")])
        await query.edit_message_text(
            "🏠 *Доступ до закладів*\nОберіть керівника, щоб налаштувати, які заклади він бачить:",
            reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
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
        await query.edit_message_text("👨‍💼 *Адмін-меню*\nОберіть дію:",
            reply_markup=build_admin_menu_keyboard(user_id), parse_mode="Markdown")

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
            "✍️ Надішліть інструкцію, що змінити — *текстом або голосом*.",
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
        await query.edit_message_text("❌ Редагування скасовано.\n\n👨‍💼 *Адмін-меню*\nОберіть дію:",
            reply_markup=build_admin_menu_keyboard(user_id), parse_mode="Markdown")

async def show_unresolved(query, context):
    """Показує невиконані завдання з повною клавіатурою і історією."""
    uid = query.from_user.id
    bot = get_bot()
    try:
        venues = visible_restaurants(uid)
        rows = [_task_record(r) for _, r in tasks_store.items() if str(r.get("fid", "")).strip()]
        if venues != set(RESTAURANTS):
            rows = [r for r in rows if r.get("Заклад") in venues]
        unresolved = [r for r in rows if not _is_resolved(r.get("Статус", ""))]
        multi_venue = (venues == set(RESTAURANTS)) or len(venues) > 1
        if multi_venue:
            unresolved.sort(key=lambda r: (r.get("Заклад", ""), _urg_rank(r.get("Терміновість", "")), -_row_age_days(r)))
        else:
            unresolved.sort(key=lambda r: (_urg_rank(r.get("Терміновість", "")), -_row_age_days(r)))

        if not unresolved:
            await query.edit_message_text(
                "✅ *Всі завдання виконано!*",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="menu_back")]]))
            return

        # Очищуємо чат перед показом актуального списку задач
        await delete_tracked_messages(bot, [uid])

        header = await safe_send_message(bot, uid, f"🔴 *Невиконані завдання: {len(unresolved)}*",
                                         reply_markup=get_main_keyboard(uid), parse_mode="Markdown")
        if header:
            track_msg(uid, header.message_id)

        cur_venue = None
        for row in unresolved:
            try:
                fid = row.get("Номер", "—")
                safe_id = fid.replace("-", "_")
                if multi_venue and row.get("Заклад") != cur_venue:
                    cur_venue = row.get("Заклад")
                    hv = await safe_send_message(bot, uid, f"🏠 *{md(cur_venue or '—')}*", parse_mode="Markdown")
                    if hv:
                        track_msg(uid, hv.message_id)
                _, keyboard = build_row_text(row, fid, safe_id)
                raw = msg_store.get(fid)
                stored_text = raw.get("text", "") if isinstance(raw, dict) else ""
                stored_photo = raw.get("photo") if isinstance(raw, dict) else None
                stored_photo_path = raw.get("photo_path", "") if isinstance(raw, dict) else ""
                text = stored_text if stored_text else build_row_text(row, fid, safe_id)[0]
                msg = await safe_send_message(bot, uid, text, reply_markup=keyboard, parse_mode="Markdown")
                if msg is None:
                    continue
                track_msg(uid, msg.message_id)
                schedule_delete(context, bot, uid, msg.message_id, delay=TASK_DELETE_DELAY)
                if stored_photo:
                    photo_msg = await safe_send_photo(bot, uid, file_id=stored_photo, file_path=stored_photo_path)
                    if photo_msg is not None:
                        track_msg(uid, photo_msg.message_id)
                        schedule_delete(context, bot, uid, photo_msg.message_id, delay=TASK_DELETE_DELAY)
                _store_task_message(fid, uid, msg.message_id, text, photo=stored_photo, photo_path=stored_photo_path)
                if fid != "—":
                    storage.kv_put("msgstore", fid, msg_store[fid])
                await asyncio.sleep(0.03)
            except Exception as e:
                logger.error(f"Unresolved item {row.get('Номер')} for {uid}: {e}")

    except Exception as e:
        logger.error(f"Show unresolved error: {e}")
        try:
            await query.message.reply_text("❌ Помилка при завантаженні завдань.")
        except Exception:
            pass


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
        bot = get_bot()
        try:
            await bot.send_message(chat_id=user_id, text="✅ Ваш доступ до бота відновлено! Ласкаво просимо назад 🎉", reply_markup=get_main_keyboard(user_id))
        except Exception as e:
            logger.error(f"Restore notify error: {e}")

async def handle_admin_role_select(update, context):
    query = update.callback_query
    await safe_answer(query)
    if not is_admin(update.effective_user.id):
        await safe_answer(query, "Немає прав.", show_alert=True)
        return
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
    actor_id = update.effective_user.id
    if not is_admin(actor_id):
        await safe_answer(query, "Немає прав.", show_alert=True)
        return
    parts = query.data.replace("adminsetrole_", "").split("_", 1)
    target_uid = int(parts[0])
    new_role = parts[1]
    # лише власник може призначати керівні ролі (захист від самопідвищення)
    if new_role in ADMIN_ROLES and not is_owner(actor_id):
        await safe_answer(query, "Лише власник може призначати керівні ролі.", show_alert=True)
        return
    if target_uid in user_profiles:
        user_profiles[target_uid]["role"] = new_role
        save_profiles(user_profiles)
        full_name = get_display_name(user_profiles[target_uid])
        await query.edit_message_text(f"✅ Роль *{full_name}* змінено на *{new_role}*", parse_mode="Markdown")
        bot = get_bot()
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
        # прибираємо доручення на звільненого, щоб він не міг діяти на старих задачах
        removed = [fid for fid, info in list(assign_store.items()) if info.get("assignee_id") == user_id]
        for fid in removed:
            assign_store.pop(fid, None)
        if removed:
            save_assign_store(assign_store)
        full_name = get_display_name(user_profiles[user_id])
        await query.edit_message_text(f"✅ {full_name} — звільнено.")
        await safe_send_message(get_bot(), user_id, "❌ Ваш доступ до бота відхилено керівником.", parse_mode=None)

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
    bot = get_bot()
    text = (f"🔄 *Заявка на зміну ролі*\n\n👤 {full_name}\n"
            f"📌 Поточна: {profile.get('role', '—')}\n➡️ Нова: {new_role}\n🏠 {profile.get('restaurant', '—')}")
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Підтвердити", callback_data=f"confirmrole_{user_id}_{new_role}"),
        InlineKeyboardButton("❌ Відхилити", callback_data=f"rejectrole_{user_id}"),
    ]])
    for admin_id in get_recipients():
        try:
            msg = await bot.send_message(chat_id=admin_id, text=text, reply_markup=keyboard, parse_mode="Markdown")
            track_msg(admin_id, msg.message_id)
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
        bot = get_bot()
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
        bot = get_bot()
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

_last_error_notify = 0.0

async def error_handler(update, context):
    """Глобальний обробник — жодна помилка більше не зникає тихо."""
    global _last_error_notify
    logger.error("Unhandled exception while handling update", exc_info=context.error)
    try:
        now = time.time()
        if OWNER_IDS and (now - _last_error_notify) > 60:   # не частіше разу на хвилину
            _last_error_notify = now
            err = str(context.error)[:300]
            await safe_send_message(get_bot(), OWNER_IDS[0], f"⚠️ Помилка бота:\n{err}", parse_mode=None)
    except Exception:
        pass


async def _post_init(app):
    """Після завантаження persistence: прибрати тимчасовий стан, щоб старе фото/опис
    не причепились до нового повідомлення після перезапуску бота."""
    transient = ('pending_photo', 'pending_message', 'message_type', 'waiting_for_option',
                 'waiting_for_photo', 'option_message_id', 'option_chat_id', 'is_anonymous',
                 'sender_name', 'sender_role', 'commenting_on', 'comment_safe_id',
                 'comment_prompt_msg_id', 'comment_prompt_chat_id',
                 'report_mode', 'report_anonymous', 'report_prompt_msg_id', 'report_prompt_chat_id',
                 'prompt_editing', 'prompt_working', 'prompt_original', 'prompt_msg_id', 'prompt_chat_id')
    try:
        for ud in app.user_data.values():
            for k in transient:
                ud.pop(k, None)
        logger.info("post_init: transient user_data cleared")
    except Exception as e:
        logger.error(f"post_init cleanup error: {e}")

    # Одноразова міграція задач із Google Sheet у SQLite (якщо БД задач порожня)
    global tasks_store
    try:
        if storage.tasks_count() == 0:
            recs = await _arun(lambda: _sheet_retry(_read_records))
            migrated, seq = [], 0
            for r in reversed(recs):  # лист новіші згори -> старим даємо менший seq
                fid = str(r.get("Номер", "")).strip()
                if not fid:
                    continue
                seq += 1
                migrated.append({
                    "fid": fid, "created_date": r.get("Дата", ""), "created_time": r.get("Час", ""),
                    "restaurant": r.get("Заклад", ""), "sender_name": r.get("Хто повідомив", "—"),
                    "sender_role": r.get("Роль", "—"), "category": r.get("Категорія", "—"),
                    "summary": r.get("Суть", "—"), "responsible": r.get("Відповідальний", "—"),
                    "urgency": r.get("Терміновість", "—"),
                    "has_photo": str(r.get("Фото", "")).strip().startswith("є"),
                    "status": (r.get("Статус", "Нове") or "Нове"), "log": r.get("Лог", ""), "seq": seq,
                })
            storage.bulk_save_tasks(migrated)
            logger.info(f"post_init: migrated {len(migrated)} tasks from Sheet into SQLite")
    except Exception as e:
        logger.error(f"post_init task migration error: {e}")
    # Перезавантажуємо кеш задач із БД (після можливої міграції)
    try:
        tasks_store = storage.load_tasks()
        logger.info(f"post_init: tasks_store loaded ({len(tasks_store)})")
    except Exception as e:
        logger.error(f"post_init tasks load error: {e}")


def _result_from_rec(rec):
    return {"category": rec.get("category", "—"), "summary": rec.get("summary", "—"),
            "restaurant": rec.get("restaurant", "Терраса"), "urgency": rec.get("urgency", "Стандартна"),
            "responsible": rec.get("responsible", "—")}

async def sla_sweep(context):
    """Періодично: (1) дослати термінові задачі без живої копії (недоставлені);
    (2) ескалювати власнику прострочені термінові без реакції. Тихо, поки нічого не зривається."""
    try:
        bot = get_bot()
        for fid, rec in list(tasks_store.items()):
            if _is_resolved(rec.get("status", "")):
                continue
            urg = rec.get("urgency", "Стандартна")
            entry = msg_store.get(fid)
            live = bool(isinstance(entry, dict) and entry.get("ids"))
            if urg in REALTIME_URGENCY and not live:   # недоставлена термінова → дослати
                ud = {"sender_name": rec.get("sender_name", "—"), "sender_role": rec.get("sender_role", "—")}
                await send_notifications(_result_from_rec(rec), ud, {"restaurant": rec.get("restaurant")},
                    photo_file_id=(entry or {}).get("photo"), feedback_id=fid, context=context,
                    photo_path=(entry or {}).get("photo_path", ""), send_live_photo=False)
            if (urg in ESCALATE_URGENCY and rec.get("created_ts")   # лише задачі, створені вже з трекінгом
                    and not rec.get("acknowledged_at")
                    and not rec.get("escalated") and _task_age_hours(rec) >= SLA_HOURS.get(urg, 24)):
                for oid in OWNER_IDS:
                    await safe_send_message(bot, oid,
                        f"⏰ Прострочено (термінова, ~{int(_task_age_hours(rec))}год без реакції):\n"
                        f"`{fid}` · {md(rec.get('restaurant',''))} · {md(rec.get('summary',''))}",
                        parse_mode="Markdown")
                rec["escalated"] = 1
                storage.save_task(rec)
    except Exception as e:
        logger.error(f"SLA sweep error: {e}")

async def daily_backup(context):
    """Щоденний узгоджений бекап feedback.db на E: і G: з ротацією (3-2-1)."""
    try:
        await _arun(storage.checkpoint)
    except Exception:
        pass
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    made = []
    for d in BACKUP_DIRS:
        try:
            os.makedirs(d, exist_ok=True)
            dest = os.path.join(d, f"feedback_{stamp}.db")
            await _arun(lambda dest=dest: storage.backup_to(dest))
            if await _arun(lambda dest=dest: storage.integrity_ok(dest)):
                made.append(dest)
                files = sorted(f for f in os.listdir(d) if f.startswith("feedback_") and f.endswith(".db"))
                for old in files[:-BACKUP_RETENTION]:
                    try:
                        os.remove(os.path.join(d, old))
                    except Exception:
                        pass
            else:
                logger.error(f"Backup integrity failed: {dest}")
        except Exception as e:
            logger.error(f"Backup to {d} failed: {e}")
    if made:
        logger.info(f"DB backup ok ({len(made)} dest)")
    elif OWNER_IDS:
        await safe_send_message(get_bot(), OWNER_IDS[0], "⚠️ Не вдалося зробити бекап БД сьогодні.", parse_mode=None)

async def checkpoint_job(context):
    """Періодично зливає WAL у основний файл (щоб дані не накопичувались лише у WAL)."""
    await _arun(storage.checkpoint)


def main():
    global application
    asyncio.set_event_loop(asyncio.new_event_loop())

    # Persistence: pending_photo / pending_message переживають перезапуск бота
    pkl_path = os.path.join(os.path.dirname(__file__), "bot_state.pkl")
    if os.path.exists(pkl_path):
        valid = False
        try:
            import pickle
            with open(pkl_path, "rb") as f:
                data = pickle.load(f)
            # PTB-сумісність: має бути dict із ключами, які читає PicklePersistence
            valid = isinstance(data, dict) and {"user_data", "chat_data", "conversations"} <= set(data.keys())
        except Exception as e:
            logger.error(f"bot_state.pkl unreadable: {e}")
        if not valid:
            logger.error("bot_state.pkl invalid/incompatible — removing to avoid crash-loop")
            try:
                os.remove(pkl_path)
            except Exception:
                pass

    # Прибираємо осиротілі LOCAL-задачі (створені під час недоступності таблиці) —
    # вони не реконсиляться з таблицею, тож не накопичуємо їх між перезапусками.
    _local = [k for k in list(msg_store.keys()) if str(k).startswith("LOCAL-")]
    if _local:
        for k in _local:
            msg_store.pop(k, None)
        save_msg_store(msg_store)
        logger.info(f"Pruned {len(_local)} orphaned LOCAL- task(s) from msg_store")

    persistence = PicklePersistence(filepath=pkl_path)
    app = (Application.builder().token(BOT_TOKEN)
           .persistence(persistence).post_init(_post_init).build())
    application = app   # щоб get_bot() використовував єдиний керований bot

    job_queue = app.job_queue
    job_queue.run_daily(send_unresolved_digest, time=datetime.strptime("06:00", "%H:%M").time())
    job_queue.run_daily(send_birthday_greetings, time=datetime.strptime("06:00", "%H:%M").time())
    job_queue.run_daily(daily_backup, time=datetime.strptime("05:30", "%H:%M").time())   # бекап БД
    job_queue.run_repeating(sla_sweep, interval=1800, first=300)        # ескалація/недоставлені, кожні 30 хв
    job_queue.run_repeating(checkpoint_job, interval=600, first=600)    # WAL checkpoint, кожні 10 хв

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
    app.add_handler(CommandHandler("shift", enter_shift_report_mode))
    app.add_handler(CallbackQueryHandler(handle_report_anon, pattern="^report_anon$"))
    app.add_handler(CallbackQueryHandler(handle_report_cancel, pattern="^report_cancel$"))
    app.add_handler(CallbackQueryHandler(handle_report_to_task, pattern="^r2task_"))
    app.add_handler(CallbackQueryHandler(handle_admin_menu, pattern="^menu_"))
    app.add_handler(CallbackQueryHandler(handle_admin_role_select, pattern="^adminrole_"))
    app.add_handler(CallbackQueryHandler(handle_admin_set_role, pattern="^adminsetrole_"))
    app.add_handler(CallbackQueryHandler(handle_venue_user, pattern="^venueuser_"))
    app.add_handler(CallbackQueryHandler(handle_venue_toggle, pattern="^vtoggle_"))
    app.add_handler(CallbackQueryHandler(handle_venue_all, pattern="^vall_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receive_text))
    app.add_handler(MessageHandler(filters.VOICE, receive_voice))

    app.add_error_handler(error_handler)

    logger.info("Бот запущено...")
    try:
        app.run_polling()
    finally:
        storage.close()   # зливаємо WAL у основний файл при коректній зупинці

if __name__ == "__main__":
    main()
