import requests
import hashlib
import xml.etree.ElementTree as ET
from datetime import datetime

SERVER = "https://terassa-chain.syrve.online"
LOGIN = "API"
PASSWORD = "1234"
TODAY = datetime.now().strftime("%d.%m.%Y")

# Авторизация
pwd_hash = hashlib.sha1(PASSWORD.encode("utf-8")).hexdigest()
r = requests.get(f"{SERVER}/resto/api/auth", params={"login": LOGIN, "pass": pwd_hash}, timeout=15)
try:
    token = r.json().get("token")
except:
    token = r.text.strip().strip('"')
print(f"✅ Токен: {str(token)[:20]}...\n")

# ============================================================
# ТЕСТ 1: Открытые заказы через OLAP
# ============================================================
print("=" * 60)
print("ТЕСТ 1: OLAP — открытые заказы (OrderDeleted=NOT_DELETED)")
print("=" * 60)
try:
    r = requests.get(f"{SERVER}/resto/api/reports/olap", params={
        "key": token, "report": "SALES",
        "from": TODAY, "to": TODAY,
        "groupRow": ["TableNum", "OrderNum", "DishName"],
        "agr": "DishDiscountSumInt,DishAmountInt"
    }, timeout=30)
    root = ET.fromstring(r.text)
    rows = root.findall("r")
    print(f"Найдено строк: {len(rows)}")
    for row in rows[:20]:
        table  = row.findtext("TableNum", "?")
        order  = row.findtext("OrderNum", "?")
        dish   = row.findtext("DishName", "?")
        amount = row.findtext("DishAmountInt", "0")
        summ   = row.findtext("DishDiscountSumInt", "0")
        print(f"  Стол {table} | Чек #{order} | {dish} x{amount} = {float(summ or 0):,.0f} ₴")
except Exception as e:
    print(f"  Ошибка: {e}")

# ============================================================
# ТЕСТ 2: Сводный отчёт по открытым заказам
# ============================================================
print()
print("=" * 60)
print("ТЕСТ 2: ORDERS_SUMMARY_REPORT — открытые заказы")
print("=" * 60)
try:
    r = requests.get(f"{SERVER}/resto/api/reports/olap", params={
        "key": token, "report": "SALES",
        "from": TODAY, "to": TODAY,
        "groupRow": ["TableNum", "OrderNum"],
        "agr": "DishDiscountSumInt,GuestNum"
    }, timeout=30)
    root = ET.fromstring(r.text)
    rows = root.findall("r")
    print(f"Найдено заказов: {len(rows)}")
    for row in rows[:20]:
        table  = row.findtext("TableNum", "?")
        order  = row.findtext("OrderNum", "?")
        summ   = row.findtext("DishDiscountSumInt", "0")
        guests = row.findtext("GuestNum", "0")
        print(f"  Стол {table} | Чек #{order} | Сумма: {float(summ or 0):,.0f} ₴ | Гостей: {guests}")
except Exception as e:
    print(f"  Ошибка: {e}")

# ============================================================
# ТЕСТ 3: Прямой endpoint для заказов
# ============================================================
print()
print("=" * 60)
print("ТЕСТ 3: Прямые endpoints для открытых заказов")
print("=" * 60)
endpoints = [
    "/resto/api/v2/cashshifts/list",
    "/resto/api/orders",
    "/resto/api/v2/orders",
    "/resto/api/v2/orders/activeOrders",
    "/resto/api/front/orders",
]
for ep in endpoints:
    try:
        r = requests.get(f"{SERVER}{ep}", params={"key": token}, timeout=10)
        print(f"  [{r.status_code}] {ep} → {r.text[:100]}")
    except Exception as e:
        print(f"  [ERR] {ep} → {e}")

# ============================================================
# ТЕСТ 4: Незакрытые смены (активные столы)
# ============================================================
print()
print("=" * 60)
print("ТЕСТ 4: Активные кассовые смены")
print("=" * 60)
try:
    r = requests.get(f"{SERVER}/resto/api/v2/cashshifts/list", params={
        "key": token,
        "openDateFrom": TODAY,
        "openDateTo": TODAY,
        "status": "OPEN"
    }, timeout=15)
    print(f"  Статус: {r.status_code}")
    print(f"  Ответ: {r.text[:300]}")
except Exception as e:
    print(f"  Ошибка: {e}")

print("\n✅ Тест завершён!")
