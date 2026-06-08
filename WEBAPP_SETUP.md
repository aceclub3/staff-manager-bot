# Пульт керівника (Telegram Mini App) — налаштування

Дашборд менеджера/власника **усередині Telegram**, поверх наявного бота. Персонал
і далі пише в звичайний чат — дашборд лише для тих, у кого `can_manage_tasks`
(Управляючий / Виконавчий директор / Адміністратор / власник).

Технічно: невеликий `aiohttp`-сервер живе **в тому ж процесі**, що й `bot.py`
(спільні кеші, БД, корутини). Дії в дашборді викликають ТІ САМІ корутини, що й
кнопки в чаті, тож чат і дашборд завжди синхронні. Деталі — у `logs/_miniapp_design.md`.

---

## Що вже зроблено в коді
- `webapp_api.py` — API + автентифікація Telegram WebApp (initData HMAC).
- `webapp/` — фронтенд (index.html / app.js / style.css), без білд-кроку.
- `bot.py` — піднімає сервер у `post_init`, зупиняє у `post_shutdown`; додає кнопку
  «📊 Пульт керівника» в адмін-меню (показується лише якщо задано `WEBAPP_URL`).
- Бібліотека `aiohttp` — уже встановлена.

Лишилось дві речі: (1) дати серверу **публічний HTTPS-адрес** (вимога Telegram),
(2) прописати його в `.env` і перезапустити бот.

---

## ✅ Як це РОЗГОРНУТО зараз: Tailscale Funnel (2026-06-08)

Фактично використано **Tailscale Funnel** (без домену, безкоштовно), а не Cloudflare.
Cloudflare-варіант лишено нижче як альтернативу.

- Tailscale встановлено на сервері (`C:\Program Files\Tailscale\`), увійшли акаунтом **aceclub3 (GitHub)**.
- Funnel піднято на порт дашборду:
  ```powershell
  & "C:\Program Files\Tailscale\tailscale.exe" funnel --bg 8081
  ```
  (одноразово треба було ввімкнути Funnel/HTTPS за посиланням, що видала команда).
- Публічний адрес: **`https://server.tail7f319a.ts.net`** → проксі на `http://127.0.0.1:8081`.
- У `.env`: `WEBAPP_URL=https://server.tail7f319a.ts.net`.
- Конфіг Funnel **персистентний** (зберігається tailscaled) — підніметься сам разом зі службою Tailscale після перезавантаження сервера. Повторно команди не потрібні.
- Перевірка: `https://server.tail7f319a.ts.net/health` → `{"ok": true}`.
- Керування: `tailscale funnel status` (показати), `tailscale funnel --https=443 off` (вимкнути).

> Якщо колись зміниться ім'я машини/tailnet — оновити `WEBAPP_URL` у `.env` і перезапустити бот.

---

## Крок 1. Змінні в `.env`

```ini
# Пульт керівника
WEBAPP_ENABLED=1            # 1 = вмикати сервер (типово). 0 = вимкнути зовсім.
WEBAPP_HOST=127.0.0.1       # слухаємо ЛИШЕ локально (назовні — тільки через тунель)
WEBAPP_PORT=8081            # локальний порт
WEBAPP_URL=                 # ПУБЛІЧНИЙ https-URL (заповнити після кроку 2)
```

Поки `WEBAPP_URL` порожній — сервер працює локально, але **кнопки в меню немає**
(бо Telegram відкриває WebApp лише з валідного https). Заповните після кроку 2.

---

## Крок 2 (АЛЬТЕРНАТИВА, не використано). Публічний HTTPS через Cloudflare Tunnel

Telegram вимагає, щоб сторінка Mini App відкривалась по **https з валідним
сертифікатом**. Cloudflare Tunnel дає це безкоштовно, без білого IP і проброса портів.

### Встановлення `cloudflared` (раз)
1. Завантажте `cloudflared-windows-amd64.exe` зі сторінки релізів Cloudflare,
   перейменуйте на `cloudflared.exe`, покладіть, напр., у `C:\bots\cloudflared.exe`.

### Варіант A — швидко спробувати (тимчасовий URL, без домену)
```powershell
C:\bots\cloudflared.exe tunnel --url http://127.0.0.1:8081
```
Видасть тимчасовий URL виду `https://<random>.trycloudflare.com`. Скопіюйте його в
`WEBAPP_URL`, перезапустіть бот (крок 3) — і можна тестувати з телефону.
⚠️ Цей URL **змінюється при кожному запуску** `cloudflared`, тож для постійного
користування зробіть Варіант B.

### Варіант B — стабільно (свій домен на Cloudflare)
Потрібен домен, доданий у Cloudflare (DNS керується там).
```powershell
# 1) авторизація (відкриє браузер, оберіть свій домен)
C:\bots\cloudflared.exe tunnel login

# 2) створити іменований тунель (збереже credentials .json у %USERPROFILE%\.cloudflared\)
C:\bots\cloudflared.exe tunnel create feedback-pult

# 3) прив'язати піддомен до тунелю
C:\bots\cloudflared.exe tunnel route dns feedback-pult pult.ВАШ-ДОМЕН.com
```
Створіть конфіг `%USERPROFILE%\.cloudflared\config.yml`:
```yaml
tunnel: feedback-pult
credentials-file: C:\Users\M.Denys\.cloudflared\<TUNNEL-ID>.json
ingress:
  - hostname: pult.ВАШ-ДОМЕН.com
    service: http://127.0.0.1:8081
  - service: http_status:404
```
Запуск як служба Windows (підніматиметься з системою):
```powershell
C:\bots\cloudflared.exe service install
Start-Service cloudflared
```
Тоді `WEBAPP_URL=https://pult.ВАШ-ДОМЕН.com`.

> Замість Cloudflare можна будь-який reverse-proxy з валідним TLS (Caddy з
> авто-Let's Encrypt тощо), що проксує `https://…` → `http://127.0.0.1:8081`.

---

## Крок 3. Прописати URL і перезапустити бот
1. `WEBAPP_URL=https://…` (з кроку 2) у `.env`.
2. Перезапуск бота (елевована сесія): `C:\bots\restart_all_bots.bat` від імені
   адміністратора. Ознака успіху в `logs\output_*.log`:
   - `post_init: transient user_data cleared`
   - `webapp_api: Пульт керівника слухає http://127.0.0.1:8081`

---

## Крок 4. Як відкрити (менеджеру)
У чаті з ботом → reply-кнопка **«👨‍💼 Адмін-меню»** → inline-кнопка
**«📊 Пульт керівника»**. Відкриється всередині Telegram, бот автоматично впізнає
користувача (initData) і покаже його заклади.

> (Опц.) Можна зробити постійну кнопку «☰» біля поля вводу через BotFather:
> `/setmenubutton` → URL = `WEBAPP_URL`. Але вона буде в усіх користувачів; саме тому
> ми додали кнопку в адмін-меню (вона рольова). Доступ усе одно захищений на сервері.

---

## Безпека
- Сервер слухає лише `127.0.0.1`; назовні — тільки через тунель.
- Кожен запит перевіряє підпис `initData` (HMAC-SHA256 токеном бота) і свіжість (24 год).
- Усі права — на сервері: `/api/*` лише для `can_manage_tasks`; дії над задачею —
  `can_act_on_task` за закладом; персонал — `is_admin`; видалення та керівні ролі — `is_owner`.
- Фото віддається лише по `fid` і лише з дозволених тек (E:/G:), не по довільному шляху.

## Якщо щось не працює
- Кнопки немає в меню → `WEBAPP_URL` порожній або бот не перезапущено.
- «🔒 Не авторизовано» у вебі → відкрито не через Telegram (initData відсутній), або
  токен у `.env` не той, що в боті.
- «Немає доступу» → користувач не `can_manage_tasks` (звичайний персонал — це норма).
- У логах немає рядка про `webapp_api … слухає` → дивись помилку поряд; бот працює
  й без дашборду (сервер best-effort, бот не падає через нього).
- Порт `8081` зайнятий → зміни `WEBAPP_PORT` і `service:` у config.yml тунелю.
