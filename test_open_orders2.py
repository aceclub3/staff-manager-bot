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
# ТЕСТ 1: Все возможные поля суммы для открытых заказов
# ============================================================
print("=" * 60)
print("ТЕСТ 1: Запрашиваем ВСЕ поля суммы сразу")
print("=" * 60)
try:
    r = requests.get(f"{SERVER}/resto/api/reports/olap", params={
        "key": token,
        "report": "SALES",
        "from": TODAY,
        "to": TODAY,
        "groupRow": ["TableNum", "OrderNum"],
        "agr": [
            "DishSumInt",
            "DishDiscountSumInt",
            "DiscountSum",
            "IncreaseSum",
            "DishAmountInt",
            "GuestNum",
        ]
    }, timeout=30)
    root = ET.fromstring(r.text)
    rows = root.findall("r")
    print(f"Найдено строк: {len(rows)}")
    print()
    for row in rows[:15]:
        table  = row.findtext("TableNum", "?")
        order  = row.findtext("OrderNum", "?")
        f1 = row.findtext("DishSumInt", "—")
        f2 = row.findtext("DishDiscountSumInt", "—")
        f3 = row.findtext("DiscountSum", "—")
        f4 = row.findtext("IncreaseSum", "—")
        f5 = row.findtext("DishAmountInt", "—")
        f6 = row.findtext("GuestNum", "—")
        print(f"  Стол {table} | Чек #{order}")
        print(f"    DishSumInt:         {f1}")
        print(f"    DishDiscountSumInt: {f2}")
        print(f"    DiscountSum:        {f3}")
        print(f"    IncreaseSum:        {f4}")
        print(f"    DishAmountInt:      {f5}")
        print(f"    GuestNum:           {f6}")
        print()
except Exception as e:
    print(f"Ошибка: {e}")

# ============================================================
# ТЕСТ 2: Посмотрим все XML теги первой строки
# ============================================================
print("=" * 60)
print("ТЕСТ 2: Все XML теги которые возвращает API (первые 3 строки)")
print("=" * 60)
try:
    r = requests.get(f"{SERVER}/resto/api/reports/olap", params={
        "key": token,
        "report": "SALES",
        "from": TODAY,
        "to": TODAY,
        "groupRow": ["TableNum", "OrderNum", "DishName"],
        "agr": "DishSumInt,DishDiscountSumInt,DishAmountInt"
    }, timeout=30)
    root = ET.fromstring(r.text)
    rows = root.findall("r")
    print(f"Всего строк: {len(rows)}")
    print()
    for row in rows[:3]:
        print("  --- Строка ---")
        for child in row:
            print(f"    <{child.tag}> = '{child.text}'")
        print()
except Exception as e:
    print(f"Ошибка: {e}")

# ============================================================
# ТЕСТ 3: Попробуем report=ORDERS
# ============================================================
print("=" * 60)
print("ТЕСТ 3: report=ORDERS (другой тип отчёта)")
print("=" * 60)
try:
    r = requests.get(f"{SERVER}/resto/api/reports/olap", params={
        "key": token,
        "report": "ORDERS",
        "from": TODAY,
        "to": TODAY,
        "groupRow": "TableNum",
        "agr": "DishDiscountSumInt"
    }, timeout=30)
    print(f"Статус: {r.status_code}")
    print(f"Ответ: {r.text[:500]}")
except Exception as e:
    print(f"Ошибка: {e}")

print("\n✅ Тест завершён!")
