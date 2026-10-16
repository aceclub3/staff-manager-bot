"""Пароль до бек-офісу iiko-ap через цей бот (задача 212 проєкту iiko-ap).

Навіщо: перевіряючим (Кокош, Гузак) потрібен вхід у бек-офіс, що не спирається на PIN iiko — PIN-и
в закладі знають усі. Людина сама задає собі пароль, а номер телефону доводить Telegram.

Потік (особистий чат, лише активний профіль):
  /password → кнопка «📱 Підтвердити номер» (request_contact) → контакт мусить бути САМЕ цього
  акаунта (contact.user_id == від кого повідомлення) і збігатись із номером у профілі бота →
  POST в едж iiko-ap `staff-password` {action:'issue'} із секретом із .env → едж звіряє номер зі
  списком дозволених і повертає одноразове посилання на 15 хв → бот шле його (і прибирає з чату
  за 15 хв). Сам пароль людина вводить уже на сторінці iiko-ap — через Telegram він НЕ проходить.

Чому не «напишіть пароль сюди, бот видалить»: повідомлення проходить через сервери Telegram і
лишається в сповіщеннях/на інших пристроях людини навіть після видалення; посилання дає ще й
збереження пароля менеджером паролів на правильному сайті та одразу Face ID (паскі).

Модуль НЕ імпортує bot.py: живий модуль інʼєктується через init() (як webapp_api).
Конфіг (.env): IIKO_STAFF_PW_SECRET, IIKO_STAFF_PW_URL.
"""
import os
import re
import time

import httpx
from telegram import KeyboardButton, ReplyKeyboardMarkup

B = None  # живий модуль bot.py

CMD = "password"
CONFIRM_BTN = "📱 Підтвердити номер"
CANCEL_BTN = "✖️ Не зараз"
STATE_KEY = "iiko_pw"
STATE_TTL = 300            # 5 хв на те, щоб натиснути кнопку
LINK_DELETE_AFTER = 15 * 60  # посилання живе 15 хв — стільки ж і в чаті

REASON_TEXT = {
    "not_allowed": "Ваш номер не в списку тих, кому відкрито вхід у бек-офіс iiko. Якщо це помилка — зверніться до власника.",
    "rate_limited": "Забагато спроб. Спробуйте ще раз за годину.",
    "rate_limited_global": "Забагато спроб. Спробуйте ще раз за годину.",
    "no_account": "Для вас ще не створено доступ до бек-офісу — зверніться до власника.",
    "banned": "Доступ до бек-офісу для вас закрито — зверніться до власника.",
    "bad_phone": "Не вдалося розпізнати номер. Потрібен український номер +380.",
}
FAIL_TEXT = "Сервіс тимчасово недоступний. Спробуйте трохи пізніше."


def init(bot_module):
    global B
    B = bot_module


def norm_phone_ua(raw):
    """Той самий розбір, що й normPhoneUA у iiko-ap (_shared/staffLogin.js)."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or re.search(r"[^\d\s+()\-.]", s):
        return None
    d = re.sub(r"\D", "", s)
    n = None
    if len(d) == 12 and d.startswith("380"):
        n = d[3:]
    elif len(d) == 11 and d.startswith("80"):
        n = d[2:]
    elif len(d) == 10 and d.startswith("0"):
        n = d[1:]
    elif len(d) == 9 and not s.startswith("+"):
        n = d
    if not n or len(n) != 9 or n.startswith("0"):
        return None
    return "+380" + n


async def _post(payload):
    """Виклик еджа. Повертає (status, json). Окрема функція — тести підміняють її."""
    url = os.getenv("IIKO_STAFF_PW_URL", "").strip()
    secret = os.getenv("IIKO_STAFF_PW_SECRET", "").strip()
    if not url or not secret:
        return 0, {}
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(url, json=payload, headers={"x-staff-secret": secret})
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, {}


def _private(update):
    return update.effective_chat is not None and update.effective_chat.type == "private"


def _active(user_id):
    return B.user_profiles.get(user_id, {}).get("status") == B.STATUS_ACTIVE


async def _say(update, context, text, keyboard=None, delete_after=None):
    bot = B.get_bot()
    msg = await B.safe_send_message(bot, update.effective_chat.id, text, reply_markup=keyboard, parse_mode=None)
    if msg is not None and delete_after:
        B.schedule_delete(context, bot, msg.chat_id, msg.message_id, delay=delete_after)
    return msg


async def _restore_keyboard(update, context, text):
    # reply-клавіатура живе лише на ПОСТІЙНОМУ повідомленні (правило 11 у CLAUDE.md бота)
    await _say(update, context, text, keyboard=B.get_main_keyboard(update.effective_user.id))


async def cmd_password(update, context):
    """/password — почати: попросити поділитися СВОЇМ контактом."""
    if not _private(update):
        return
    uid = update.effective_user.id
    bot = B.get_bot()
    B.schedule_delete(context, bot, update.effective_chat.id, update.message.message_id, delay=60)
    # бот не логує вхідних (TASKS.md У4: «мовчить із 21.08» — не розрізнити «не пишуть» і «не доходить»),
    # тож хоч ця команда лишає слід у лозі
    B.logger.info(f"iiko_password: /password від tg={uid}")
    if not _active(uid):
        await _say(update, context, "Ця команда — лише для активних працівників, зареєстрованих у боті.", delete_after=60)
        return
    context.user_data[STATE_KEY] = {"ts": time.time()}
    kb = ReplyKeyboardMarkup([[KeyboardButton(CONFIRM_BTN, request_contact=True)], [KeyboardButton(CANCEL_BTN)]],
                             resize_keyboard=True, one_time_keyboard=True)
    await _say(update, context,
               "🔑 Пароль до бек-офісу iiko.\n\nПідтвердіть, що це ваш номер: натисніть кнопку «📱 Підтвердити номер» нижче. "
               "Після цього я надішлю одноразове посилання, де ви самі задасте пароль (PIN каси для цього не потрібен).",
               keyboard=kb, delete_after=STATE_TTL)


async def on_cancel(update, context):
    if not _private(update) or not context.user_data.pop(STATE_KEY, None):
        return False
    B.schedule_delete(context, B.get_bot(), update.effective_chat.id, update.message.message_id, delay=5)
    await _restore_keyboard(update, context, "Добре, без пароля. Команда /password — коли знадобиться.")
    return True


async def on_contact(update, context):
    """Контакт поза реєстрацією. Повертає True, якщо це був наш крок (інакше бот ігнорує, як і раніше)."""
    if not _private(update):
        return False
    st = context.user_data.pop(STATE_KEY, None)
    if not st or time.time() - st.get("ts", 0) > STATE_TTL:
        return False
    uid = update.effective_user.id
    contact = update.message.contact
    bot = B.get_bot()
    # номер у чаті не лишаємо
    B.schedule_delete(context, bot, update.effective_chat.id, update.message.message_id, delay=5)
    if not _active(uid):
        await _restore_keyboard(update, context, "Ця команда — лише для активних працівників.")
        return True
    # Telegram ручається за номер лише тоді, коли людина ділиться ВЛАСНИМ контактом кнопкою;
    # пересланий чужий контакт має user_id іншої людини (або порожній)
    if contact is None or contact.user_id != uid:
        await _restore_keyboard(update, context, "Потрібен саме ваш номер — натисніть кнопку «📱 Підтвердити номер», а не надсилайте чужий контакт. Спробуйте /password ще раз.")
        return True
    phone = norm_phone_ua(contact.phone_number)
    prof_phone = norm_phone_ua(B.user_profiles.get(uid, {}).get("phone"))
    if phone is None:
        await _restore_keyboard(update, context, REASON_TEXT["bad_phone"])
        return True
    if prof_phone and prof_phone != phone:
        await _restore_keyboard(update, context, "Номер вашого Telegram не збігається з номером у вашому профілі бота. Зверніться до керівника, щоб оновили профіль.")
        return True
    try:
        status, j = await _post({"action": "issue", "phone": phone, "telegram_id": uid})
    except Exception as e:
        B.logger.error(f"iiko_password: едж недоступний: {type(e).__name__}")
        status, j = 0, {}
    if j.get("ok") and j.get("url"):
        await _say(update, context,
                   f"Ваше посилання (одноразове, діє {j.get('ttl_min', 15)} хв):\n{j['url']}\n\n"
                   "Відкрийте його, задайте пароль — і входьте в бек-офіс: телефон + пароль. "
                   "Там же можна одразу додати вхід за Face ID / відбитком.",
                   delete_after=LINK_DELETE_AFTER)
        await _restore_keyboard(update, context, "Повідомлення з посиланням зникне з чату за 15 хв. Нове — командою /password.")
        B.logger.info(f"iiko_password: посилання видано tg={uid}")
        return True
    reason = j.get("reason") or ""
    B.logger.info(f"iiko_password: відмова tg={uid} status={status} reason={reason or '-'}")
    await _restore_keyboard(update, context, REASON_TEXT.get(reason, FAIL_TEXT))
    return True
