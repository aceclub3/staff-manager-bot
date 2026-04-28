import requests
import hashlib
from datetime import datetime, timedelta

SERVER = "https://terassa-chain.syrve.online"
LOGIN = "API"
PASSWORD = "1234"

# Авторизация
pwd_hash = hashlib.sha1(PASSWORD.encode("utf-8")).hexdigest()
r = requests.get(f"{SERVER}/resto/api/auth", params={"login": LOGIN, "pass": pwd_hash}, timeout=15)
try:
    token = r.json().get("token")
except:
    token = r.text.strip().strip('"')
print(f"✅ Токен: {str(token)[:20]}...\n")

now = datetime.now()
from_time = (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S.000")
to_time = now.strftime("%Y-%m-%dT%H:%M:%S.000")

# ============================================================
# ТЕСТ 1: Метаданные — все типы событий
# ============================================================
print("=" * 60)
print("ТЕСТ 1: Все типы событий в системе")
print("=" * 60)
try:
    r = requests.get(f"{SERVER}/resto/api/events/metadata",
                     params={"key": token}, timeout=15)
    print(f"Статус: {r.status_code}")
    # Ищем события связанные с заказами и оплатой
    text = r.text
    keywords = ["order", "pay", "precheque", "bill", "close", "paid", "open"]
    lines = text.replace("><", ">\n<").split("\n")
    found = []
    for line in lines:
        for kw in keywords:
            if kw.lower() in line.lower() and "id" in line.lower():
                if line.strip() not in found:
                    found.append(line.strip())
    print(f"Найдено событий связанных с заказами: {len(found)}")
    for f in found[:30]:
        print(f"  {f}")
except Exception as e:
    print(f"Ошибка: {e}")

# ============================================================
# ТЕСТ 2: Последние события за 2 часа
# ============================================================
print()
print("=" * 60)
print(f"ТЕСТ 2: События за последние 2 часа ({from_time} — {to_time})")
print("=" * 60)
try:
    r = requests.get(f"{SERVER}/resto/api/events", params={
        "key": token,
        "from_time": from_time,
        "to_time": to_time
    }, timeout=15)
    print(f"Статус: {r.status_code}")
    text = r.text
    lines = text.replace("><", ">\n<").split("\n")
    # Ищем типы событий
    types = set()
    for line in lines:
        if "<type>" in line:
            t = line.strip().replace("<type>", "").replace("</type>", "")
            types.add(t)
    print(f"Типы событий за 2 часа: {len(types)}")
    for t in sorted(types):
        print(f"  - {t}")
    print(f"\nПервые 500 символов ответа:\n{text[:500]}")
except Exception as e:
    print(f"Ошибка: {e}")

# ============================================================
# ТЕСТ 3: Фильтр только по orderPrecheque и orderPaid
# ============================================================
print()
print("=" * 60)
print("ТЕСТ 3: События пречека и оплаты")
print("=" * 60)
try:
    r = requests.post(f"{SERVER}/resto/api/events",
        params={"key": token},
        data="""<eventsRequestData>
            <events>
                <event>orderPrecheque</event>
                <event>orderPaid</event>
                <event>orderOpened</event>
                <event>orderClosed</event>
            </events>
        </eventsRequestData>""",
        headers={"Content-Type": "application/xml"},
        timeout=15)
    print(f"Статус: {r.status_code}")
    print(f"Ответ: {r.text[:600]}")
except Exception as e:
    print(f"Ошибка: {e}")

print("\n✅ Тест завершён!")
