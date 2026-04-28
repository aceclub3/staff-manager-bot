import requests
import hashlib
import xml.etree.ElementTree as ET
from datetime import datetime

SERVER = "https://terassa-chain.syrve.online"
LOGIN = "API"
PASSWORD = "1234"
TODAY = datetime.now().strftime("%d.%m.%Y")

pwd_hash = hashlib.sha1(PASSWORD.encode()).hexdigest()
r = requests.get(f"{SERVER}/resto/api/auth", params={"login": LOGIN, "pass": pwd_hash}, timeout=15)
try:
    token = r.json().get("token")
except:
    token = r.text.strip().strip('"')
print(f"✅ Токен: {str(token)[:20]}...\n")

# Запрос с Department + Conception + OrderNum
r = requests.get(f"{SERVER}/resto/api/reports/olap", params={
    "key": token, "report": "SALES",
    "from": TODAY, "to": TODAY,
    "groupRow": ["Department", "Conception", "TableNum", "OrderNum"],
    "agr": "DishDiscountSumInt"
}, timeout=30)

root = ET.fromstring(r.text)
rows = root.findall("r")
print(f"Всего строк: {len(rows)}\n")

# Показываем все уникальные комбинации Department + Conception
combos = set()
for row in rows:
    dept = row.findtext("Department", "?")
    conc = row.findtext("Conception", "?")
    combos.add((dept, conc))

print("Все комбинации Department → Conception:")
for dept, conc in sorted(combos):
    print(f"  '{dept}' → '{conc}'")

print()
print("Первые 5 строк с суммами:")
for row in rows[:5]:
    dept  = row.findtext("Department", "?")
    conc  = row.findtext("Conception", "?")
    table = row.findtext("TableNum", "?")
    order = row.findtext("OrderNum", "?")
    summ  = row.findtext("DishDiscountSumInt", "0")
    print(f"  {dept} → {conc} | Стол {table} | Чек #{order} | {float(summ or 0):,.0f} ₴")
