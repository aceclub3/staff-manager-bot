import logging
import asyncio
import hashlib
import requests
import xml.etree.ElementTree as ET
from datetime import datetime
from telegram import Update, KeyboardButton, ReplyKeyboardMarkup
from telegram.ext import Application, MessageHandler, ContextTypes, filters

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN   = "8740942474:AAF8x6azbO9S-NJuBh0uy03Pxz-OLx532hw"
SYRVE_SERVER = "https://terassa-chain.syrve.online"
SYRVE_LOGIN  = "API"
SYRVE_PASSWORD = "1234"

# ============================================================
#  SYRVE
# ============================================================

def get_token():
    pwd_hash = hashlib.sha1(SYRVE_PASSWORD.encode()).hexdigest()
    r = requests.get(f"{SYRVE_SERVER}/resto/api/auth",
                     params={"login": SYRVE_LOGIN, "pass": pwd_hash}, timeout=15)
    try:
        return r.json().get("token")
    except:
        return r.text.strip().strip('"')

def get_open_tables(token):
    """Возвращает {order_num: {table, sum, discount_sum, dishes, guests}}"""
    today = datetime.now().strftime("%d.%m.%Y")

    # Запрос 1: суммы по чекам (только Терасса → Ресторан)
    r = requests.get(f"{SYRVE_SERVER}/resto/api/reports/olap", params={
        "key": token, "report": "SALES",
        "from": today, "to": today,
        "groupRow": ["Department", "Conception", "WaiterName", "TableNum", "OrderNum"],
        "agr": ["DishSumInt", "DishDiscountSumInt", "DiscountSum", "DishAmountInt", "GuestNum"]
    }, timeout=30)
    root = ET.fromstring(r.text)

    orders = {}
    for row in root.findall("r"):
        dept       = row.findtext("Department", "").strip()
        conception = row.findtext("Conception", "").strip()
        if dept != "Терасса" or conception != "Ресторан":
            continue
        order_num = row.findtext("OrderNum", "?")
        orders[order_num] = {
            "restaurant":   f"{dept} → {conception}",
            "waiter":       row.findtext("WaiterName", "—").strip(),
            "table":        row.findtext("TableNum", "?"),
            "sum":          float(row.findtext("DishSumInt", "0") or 0),
            "discount_sum": float(row.findtext("DishDiscountSumInt", "0") or 0),
            "discount":     float(row.findtext("DiscountSum", "0") or 0),
            "dishes_count": int(float(row.findtext("DishAmountInt", "0") or 0)),
            "guests":       int(float(row.findtext("GuestNum", "0") or 0)),
            "items":        []
        }

    # Запрос 2: состав блюд
    r2 = requests.get(f"{SYRVE_SERVER}/resto/api/reports/olap", params={
        "key": token, "report": "SALES",
        "from": today, "to": today,
        "groupRow": ["OrderNum", "DishName"],
        "agr": "DishAmountInt"
    }, timeout=30)
    root2 = ET.fromstring(r2.text)
    for row in root2.findall("r"):
        order_num = row.findtext("OrderNum", "?")
        dish  = row.findtext("DishName", "?")
        qty   = int(float(row.findtext("DishAmountInt", "0") or 0))
        if order_num in orders:
            orders[order_num]["items"].append(f"{dish} x{qty}")

    return orders

def format_check(order_num, data):
    """Форматирует одно сообщение по чеку"""
    now_str = datetime.now().strftime("%H:%M")

    lines = [f"📋 *Стол {data['table']} | Чек #{order_num}*"]
    lines.append(f"🏠 {data['restaurant']}")
    lines.append(f"🧑‍🍳 Официант: {data['waiter']}")
    lines.append("")
    lines.append(f"👥 Гостей: {data['guests']}")
    lines.append(f"🍽 Блюд: {data['dishes_count']}")

    if data["items"]:
        lines.append("🍴 *Состав:*")
        for item in data["items"]:
            lines.append(f"  • {item}")
        lines.append("")

    lines.append(f"💰 Сумма: {data['sum']:,.0f} ₴")
    if data["discount"] > 0:
        lines.append(f"💳 Со скидкой: {data['discount_sum']:,.0f} ₴")
        lines.append(f"🏷 Скидка: {data['discount']:,.0f} ₴")
    else:
        lines.append(f"💳 К оплате: {data['discount_sum']:,.0f} ₴")

    lines.append("")
    lines.append(f"🕐 {now_str}")
    return "\n".join(lines)

# ============================================================
#  TELEGRAM
# ============================================================

def main_keyboard():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("🪑 Открытые столы")]],
        resize_keyboard=True
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if text == "🪑 Открытые столы":
        await update.message.reply_text("⏳ Загружаю данные...")
        try:
            token = get_token()
            orders = get_open_tables(token)

            if not orders:
                await update.message.reply_text("✅ Открытых столов нет.")
                return

            now_str = datetime.now().strftime("%H:%M")
            await update.message.reply_text(
                f"📊 *Открытые столы на {now_str}* — всего чеков: {len(orders)}",
                parse_mode="Markdown"
            )

            # Каждый чек отдельным сообщением
            for order_num, data in sorted(orders.items(), key=lambda x: int(x[1]["table"]) if x[1]["table"].isdigit() else 999):
                msg = format_check(order_num, data)
                await update.message.reply_text(msg, parse_mode="Markdown")
                await asyncio.sleep(0.1)  # небольшая пауза чтобы не flood

        except Exception as e:
            logger.error(f"Ошибка: {e}")
            await update.message.reply_text(f"❌ Ошибка: {str(e)[:200]}")

    elif text == "/start":
        await update.message.reply_text(
            "👋 Бот открытых столов\n\nНажми кнопку чтобы получить список.",
            reply_markup=main_keyboard()
        )

def main():
    asyncio.set_event_loop(asyncio.new_event_loop())
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT, handle_message))
    logger.info("🚀 QR бот запущен!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
