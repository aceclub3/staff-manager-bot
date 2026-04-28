import requests
import hashlib
from datetime import datetime

SERVER = "https://terassa-chain.syrve.online"
LOGIN = "API"
PASSWORD = "1234"
TODAY = datetime.now().strftime("%Y-%m-%d")  # Правильный формат!

# Авторизация
pwd_hash = hashlib.sha1(PASSWORD.encode("utf-8")).hexdigest()
r = requests.get(f"{SERVER}/resto/api/auth", params={"login": LOGIN, "pass": pwd_hash}, timeout=15)
try:
    token = r.json().get("token")
except:
    token = r.text.strip().strip('"')
print(f"✅ Токен: {str(token)[:20]}...")
print(f"📅 Дата: {TODAY}\n")

# ============================================================
# ТЕСТ 1: Активные (открытые) смены сегодня
# ============================================================
print("=" * 60)
print("ТЕСТ 1: Открытые кассовые смены (status=OPEN)")
print("=" * 60)
try:
    r = requests.get(f"{SERVER}/resto/api/v2/cashshifts/list", params={
        "key": token,
        "openDateFrom": TODAY,
        "openDateTo": TODAY,
        "status": "OPEN"
    }, timeout=15)
    print(f"Статус: {r.status_code}")
    print(f"Ответ: {r.text[:1000]}")
except Exception as e:
    print(f"Ошибка: {e}")

# ============================================================
# ТЕСТ 2: Все смены сегодня (любой статус)
# ============================================================
print()
print("=" * 60)
print("ТЕСТ 2: Все смены сегодня (status=ANY)")
print("=" * 60)
try:
    r = requests.get(f"{SERVER}/resto/api/v2/cashshifts/list", params={
        "key": token,
        "openDateFrom": TODAY,
        "openDateTo": TODAY,
        "status": "ANY"
    }, timeout=15)
    print(f"Статус: {r.status_code}")
    import json
    data = r.json()
    print(f"Найдено смен: {len(data)}")
    for shift in data[:5]:
        print(f"\n  ID: {shift.get('id')}")
        print(f"  Статус: {shift.get('sessionStatus')}")
        print(f"  Открыта: {shift.get('openDate')}")
        print(f"  Закрыта: {shift.get('closeDate')}")
        print(f"  Касса №: {shift.get('cashRegNumber')}")
        print(f"  Выручка: {shift.get('payOrders')} ₴")
        print(f"  Точка продаж: {shift.get('pointOfSale')}")
except Exception as e:
    print(f"Ошибка: {e}\nОтвет: {r.text[:300]}")

# ============================================================
# ТЕСТ 3: Детали конкретной смены + платежи
# ============================================================
print()
print("=" * 60)
print("ТЕСТ 3: Закрытые смены (CLOSED) — для сравнения")
print("=" * 60)
try:
    r = requests.get(f"{SERVER}/resto/api/v2/cashshifts/list", params={
        "key": token,
        "openDateFrom": TODAY,
        "openDateTo": TODAY,
        "status": "CLOSED"
    }, timeout=15)
    print(f"Статус: {r.status_code}")
    data = r.json()
    print(f"Закрытых смен: {len(data)}")
    for shift in data[:3]:
        sid = shift.get('id')
        print(f"\n  Смена {sid[:20]}... | Выручка: {shift.get('payOrders')} ₴")
        # Детали платежей по смене
        r2 = requests.get(f"{SERVER}/resto/api/v2/cashshifts/payments/list/{sid}",
                          params={"key": token}, timeout=10)
        print(f"  Платежи статус: {r2.status_code} | {r2.text[:200]}")
except Exception as e:
    print(f"Ошибка: {e}")

print("\n✅ Тест завершён!")
