# -*- coding: utf-8 -*-
"""
Пульт керівника — HTTP API для Telegram Mini App (дашборд менеджера).

Працює В ТОМУ Ж процесі, що й bot.py (спільний event-loop, спільні кеші/БД).
Це навмисно (див. logs/_miniapp_design.md): feedback.db заблокована елевованим
процесом бота для запису, а вся логіка дій над задачами — це async-корутини в
bot.py. Тож дашборд викликає ТІ САМІ корутини (task_action_*), що й кнопки в чаті
— завдяки чому чат і дашборд завжди синхронні, без дублювання логіки.

Залежність від bot.py — через ІНʼЄКЦІЮ (init(bot_module)), а НЕ import, щоб
уникнути циклічного імпорту й проблеми «bot як __main__» (коли бот запущено як
скрипт, його модуль називається __main__, а `import bot` створив би другий
екземпляр з окремими кешами). bot.py викликає webapp_api.init(sys.modules[__name__]).

Автентифікація — Telegram WebApp initData (HMAC-SHA256 ботовим токеном). Усі
гейти прав — на сервері, дзеркалять бота (can_manage_tasks / can_act_on_task /
is_owner). Клієнту не довіряємо.
"""

import os
import json
import hmac
import hashlib
import time
import types
import logging
from urllib.parse import parse_qsl

from aiohttp import web

logger = logging.getLogger(__name__)

B = None                      # модуль бота (інʼєкція через init())
_runner = None                # aiohttp AppRunner (для зупинки)
WEBAPP_DIR = os.path.join(os.path.dirname(__file__), "webapp")
INIT_DATA_MAX_AGE = 86400     # initData приймаємо не старіший за 24 год


def init(bot_module):
    """Інʼєкція живого модуля бота (з усіма кешами/корутинами)."""
    global B
    B = bot_module


# ─── АВТЕНТИФІКАЦІЯ (Telegram WebApp initData) ────────────────────────────────

def validate_init_data(init_data, bot_token, max_age=INIT_DATA_MAX_AGE):
    """Перевіряє підпис initData від Telegram. Повертає dict користувача або None.
    Алгоритм: secret = HMAC_SHA256("WebAppData", bot_token);
              hash   = HMAC_SHA256(secret, data_check_string)."""
    if not init_data or not bot_token:
        return None
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True)
    except Exception:
        return None
    data = dict(pairs)
    received = data.pop("hash", None)
    if not received:
        return None
    # свіжість
    auth_date = data.get("auth_date")
    if auth_date:
        try:
            if max_age and (time.time() - int(auth_date)) > max_age:
                return None
        except (ValueError, TypeError):
            return None
    data_check_string = "\n".join(f"{k}={data[k]}" for k in sorted(data))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, received):
        return None
    user_raw = data.get("user")
    if not user_raw:
        return None
    try:
        user = json.loads(user_raw)
    except Exception:
        return None
    if not isinstance(user, dict) or not isinstance(user.get("id"), int):
        return None
    return user


# ─── MIDDLEWARE ───────────────────────────────────────────────────────────────

@web.middleware
async def error_mw(request, handler):
    """Будь-який виняток у хендлері -> JSON 500 (а не HTML), щоб фронт не падав."""
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except Exception as e:
        logger.error(f"webapp_api handler error {request.method} {request.path}: {e}")
        return web.json_response({"error": "internal"}, status=500)


@web.middleware
async def auth_mw(request, handler):
    """Гейт на /api/*: валідний initData + право can_manage_tasks. Інше — публічне (статика)."""
    if request.path.startswith("/api/"):
        init_data = request.headers.get("X-Telegram-Init-Data", "")
        user = validate_init_data(init_data, B.BOT_TOKEN)
        if not user:
            return web.json_response({"error": "unauthorized"}, status=401)
        uid = user["id"]
        if not B.can_manage_tasks(uid):
            return web.json_response({"error": "forbidden"}, status=403)
        request["uid"] = uid
        request["profile"] = B.user_profiles.get(uid, {})
    return await handler(request)


# ─── ДОПОМІЖНЕ ────────────────────────────────────────────────────────────────

def _api_context():
    """Легкий shim із job_queue для schedule_delete у спільних корутинах (assign/create)."""
    app = getattr(B, "application", None)
    jq = getattr(app, "job_queue", None) if app is not None else None
    return types.SimpleNamespace(job_queue=jq)


def _can_view(uid, restaurant):
    """Чи бачить користувач задачі цього закладу (власник — усі)."""
    if B.is_owner(uid):
        return True
    return restaurant in B.visible_restaurants(uid)


def _path_int(request, key):
    """Числовий сегмент шляху або None (для чистого 400 замість 500 на /api/staff/abc/...)."""
    try:
        return int(request.match_info[key])
    except (TypeError, ValueError, KeyError):
        return None


async def _json_body(request):
    try:
        return await request.json()
    except Exception:
        return {}


def _serialize_task(fid, rec, uid):
    assign = B.assign_store.get(fid, {}) if isinstance(B.assign_store.get(fid), dict) else {}
    age_h = B._task_age_hours(rec)
    r = rec.get("restaurant", "")
    return {
        "fid": fid,
        "restaurant": r,
        "category": rec.get("category", "—"),
        "category_icon": B.CATEGORY_ICONS.get(rec.get("category"), "📌"),
        "summary": rec.get("summary", "—"),
        "urgency": rec.get("urgency", "Стандартна"),
        "urgency_icon": B.URGENCY_ICONS.get(rec.get("urgency"), ""),
        "status": rec.get("status", "Нове"),
        "resolved": B._is_resolved(rec.get("status", "")),
        "responsible": rec.get("responsible", "—"),
        "sender_name": rec.get("sender_name", "—"),
        "sender_role": rec.get("sender_role", "—"),
        "assignee_id": assign.get("assignee_id"),
        "assignee_name": assign.get("assignee_name"),
        "age_hours": round(age_h, 1),
        "age_days": int(age_h // 24),
        "has_photo": bool(B._photo_deliverable(fid)),
        "created_date": rec.get("created_date", ""),
        "created_time": rec.get("created_time", ""),
        "completed_date": rec.get("completed_date") or "",
        "acknowledged": bool(rec.get("acknowledged_at")),
        "log": rec.get("log", "") or "",
        "seq": rec.get("seq", 0),
        "can_act": B.can_act_on_task(uid, r),
    }


# ─── ЕНДПОІНТИ: профіль / задачі ──────────────────────────────────────────────

async def api_me(request):
    uid = request["uid"]
    p = request["profile"]
    name = B.get_display_name(p) or ("Власник" if B.is_owner(uid) else "Керівник")
    role = p.get("role") or ("Власник" if B.is_owner(uid) else "—")
    return web.json_response({
        "uid": uid, "name": name, "role": role,
        "is_owner": B.is_owner(uid), "is_admin": B.is_admin(uid),
        "venues": sorted(B.visible_restaurants(uid)),
        "all_restaurants": list(B.RESTAURANTS),
    })


async def api_tasks(request):
    uid = request["uid"]
    status_f = request.query.get("status", "open")
    venue_f = request.query.get("venue")
    urg_f = request.query.get("urgency")
    unassigned = request.query.get("unassigned") == "1"
    items = []
    for fid, rec in B.tasks_store.items():
        r = rec.get("restaurant", "")
        if not _can_view(uid, r):
            continue
        resolved = B._is_resolved(rec.get("status", ""))
        if status_f == "open" and resolved:
            continue
        if status_f == "closed" and not resolved:
            continue
        if venue_f and r != venue_f:
            continue
        if urg_f and rec.get("urgency") != urg_f:
            continue
        if unassigned and B.assign_store.get(fid):
            continue
        items.append(_serialize_task(fid, rec, uid))
    # сорт: терміновість, потім вік (старіші згори)
    items.sort(key=lambda t: (B._urg_rank(t["urgency"]), -t["age_hours"]))
    return web.json_response({"tasks": items, "count": len(items)})


async def api_task_detail(request):
    uid = request["uid"]
    fid = request.match_info["fid"]
    rec = B.tasks_store.get(fid)
    if not rec:
        return web.json_response({"error": "not_found"}, status=404)
    if not _can_view(uid, rec.get("restaurant", "")):
        return web.json_response({"error": "forbidden"}, status=403)
    t = _serialize_task(fid, rec, uid)
    t["is_owner"] = B.is_owner(uid)
    return web.json_response({"task": t})


async def api_photo(request):
    uid = request["uid"]
    fid = request.match_info["fid"]
    rec = B.tasks_store.get(fid)
    if not rec:
        return web.json_response({"error": "not_found"}, status=404)
    if not _can_view(uid, rec.get("restaurant", "")):
        return web.json_response({"error": "forbidden"}, status=403)
    entry = B.msg_store.get(fid, {})
    if not isinstance(entry, dict):
        entry = {}
    # 1) локальний файл (E:/G:) — лише в межах дозволених тек (захист від traversal)
    path = entry.get("photo_path") or ""
    if path and os.path.exists(path):
        rp = os.path.realpath(path)
        roots = [os.path.realpath(B.PHOTO_PRIMARY_PATH), os.path.realpath(B.PHOTO_MIRROR_PATH)]
        # точна перевірка «всередині теки» (не префіксна — щоб ФотоX не зматчило Фото)
        if any(rp == root or rp.startswith(root + os.sep) for root in roots):
            return web.FileResponse(rp)
    # 2) фолбек — завантажити з Telegram за file_id
    file_id = entry.get("photo")
    if file_id:
        try:
            tg_file = await B.get_bot().get_file(file_id)
            data = bytes(await tg_file.download_as_bytearray())
            return web.Response(body=data, content_type="image/jpeg")
        except Exception as e:
            logger.error(f"photo fetch failed {fid}: {e}")
    return web.json_response({"error": "no_photo"}, status=404)


# ─── ЕНДПОІНТИ: дії над задачею (через спільні корутини bot.task_action_*) ─────

def _action_response(res):
    if res.get("ok"):
        return web.json_response({"ok": True, **{k: v for k, v in res.items() if k != "ok"}})
    err = res.get("error")
    status = {"no_perm": 403, "owner_only": 403, "already_closed": 409,
              "bad_target": 400, "empty": 400, "bad_action": 400}.get(err, 400)
    return web.json_response({"ok": False, "error": err, "status": res.get("status")}, status=status)


async def api_status(request):
    uid = request["uid"]
    fid = request.match_info["fid"]
    body = await _json_body(request)
    action = body.get("action")
    if action not in ("done", "wip", "cancel"):
        return web.json_response({"ok": False, "error": "bad_action"}, status=400)
    res = await B.task_action_status(fid, action, uid)
    return _action_response(res)


async def api_delete(request):
    uid = request["uid"]
    fid = request.match_info["fid"]
    res = await B.task_action_delete(fid, uid)
    return _action_response(res)


async def api_comment(request):
    uid = request["uid"]
    fid = request.match_info["fid"]
    body = await _json_body(request)
    res = await B.task_action_comment(fid, uid, body.get("text", ""))
    return _action_response(res)


async def api_assign(request):
    uid = request["uid"]
    fid = request.match_info["fid"]
    body = await _json_body(request)
    try:
        target_uid = int(body.get("assignee_id"))
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "error": "bad_target"}, status=400)
    res = await B.task_action_assign(fid, target_uid, uid, context=_api_context())
    return _action_response(res)


async def api_create_task(request):
    """Менеджер створює задачу з вільного тексту (Claude класифікує, як зі звичайного повідомлення)."""
    uid = request["uid"]
    p = request["profile"]
    body = await _json_body(request)
    text = (body.get("text") or "").strip()
    if not text:
        return web.json_response({"ok": False, "error": "empty"}, status=400)
    venue = body.get("restaurant") if body.get("restaurant") in B.RESTAURANTS else None
    default_rest = venue or p.get("restaurant") or "Терраса"
    prof = {"restaurant": default_rest, "role": p.get("role", "—"), "telegram_id": uid}
    user_data = {"sender_name": B.get_display_name(p) or "Керівник",
                 "sender_role": p.get("role", "—"), "is_anonymous": False}
    results = await B.analyze_with_claude(text, prof, is_anonymous=False)
    if venue:
        for r in results:
            r["restaurant"] = venue
    _, ids, _ = await B._save_and_notify(results, user_data, prof, None, _api_context())
    return web.json_response({"ok": True, "ids": ids})


# ─── ЕНДПОІНТИ: персонал і доступи ────────────────────────────────────────────

def _serialize_profile(tid, p):
    # БЕЗ phone/birthday: фронт їх не показує, а віддавати PII усім can_manage_tasks
    # (зокрема ролі «Адміністратор», якій у чаті ростер недоступний) — витік. Чат у
    # ростері/пікері теж не світить телефон/ДН. Знадобиться телефон — додати гейтом is_admin.
    return {
        "uid": tid,
        "name": B.get_display_name(p) or "—",
        "role": p.get("role", "—"),
        "status": p.get("status", "—"),
        "restaurant": p.get("restaurant", "—"),
        "venues": sorted(B._profile_venues(p)) if p.get("status") == B.STATUS_ACTIVE else [],
    }


async def api_staff(request):
    """Список персоналу. ?all=1 (адмін) — усі статуси; інакше — активні (для пікера доручення)."""
    uid = request["uid"]
    all_flag = request.query.get("all") == "1"
    if all_flag and not B.is_admin(uid):
        return web.json_response({"error": "forbidden"}, status=403)
    out = []
    for tid, p in B.user_profiles.items():
        if not all_flag and p.get("status") != B.STATUS_ACTIVE:
            continue
        out.append(_serialize_profile(tid, p))
    out.sort(key=lambda x: (x["status"] != "active", x["name"]))
    return web.json_response({"staff": out})


async def api_set_venues(request):
    uid = request["uid"]
    if not B.is_admin(uid):
        return web.json_response({"error": "forbidden"}, status=403)
    tid = _path_int(request, "tid")
    if tid is None:
        return web.json_response({"error": "bad_target"}, status=400)
    p = B.user_profiles.get(tid)
    if not p:
        return web.json_response({"error": "not_found"}, status=404)
    body = await _json_body(request)
    venues = [v for v in (body.get("venues") or []) if v in B.RESTAURANTS]
    if not venues:
        return web.json_response({"error": "empty"}, status=400)
    p["manages"] = [v for v in B.RESTAURANTS if v in venues]
    B.save_profiles(B.user_profiles)
    return web.json_response({"ok": True, "venues": sorted(B._profile_venues(p))})


async def api_set_role(request):
    uid = request["uid"]
    if not B.is_admin(uid):
        return web.json_response({"error": "forbidden"}, status=403)
    tid = _path_int(request, "tid")
    if tid is None:
        return web.json_response({"error": "bad_target"}, status=400)
    p = B.user_profiles.get(tid)
    if not p:
        return web.json_response({"error": "not_found"}, status=404)
    body = await _json_body(request)
    role = body.get("role")
    if role not in B.ROLES:
        return web.json_response({"error": "bad_role"}, status=400)
    # Призначення керівних ролей — лише власник (як у боті, розділ 5).
    if role in B.ADMIN_ROLES and not B.is_owner(uid):
        return web.json_response({"error": "owner_only"}, status=403)
    p["role"] = role
    B.save_profiles(B.user_profiles)
    try:
        await B.safe_send_message(B.get_bot(), tid,
                                  f"🔄 Вашу роль змінено на: {role}", parse_mode=None)
    except Exception:
        pass
    return web.json_response({"ok": True, "role": role})


async def _set_status(request, new_status, notify):
    uid = request["uid"]
    if not B.is_admin(uid):
        return web.json_response({"error": "forbidden"}, status=403)
    tid = _path_int(request, "tid")
    if tid is None:
        return web.json_response({"error": "bad_target"}, status=400)
    p = B.user_profiles.get(tid)
    if not p:
        return web.json_response({"error": "not_found"}, status=404)
    p["status"] = new_status
    if new_status == B.STATUS_FIRED:
        # знімаємо доручення зі звільненого (як у боті при fire)
        for fk, av in list(B.assign_store.items()):
            if isinstance(av, dict) and av.get("assignee_id") == tid:
                B.assign_store.pop(fk, None)
        B.save_assign_store(B.assign_store)
    B.save_profiles(B.user_profiles)
    if notify:
        try:
            await B.safe_send_message(B.get_bot(), tid, notify, parse_mode=None)
        except Exception:
            pass
    return web.json_response({"ok": True, "status": new_status})


async def api_approve(request):
    return await _set_status(request, B.STATUS_ACTIVE, "✅ Вашу заявку підтверджено! Вітаємо в команді.")

async def api_reject(request):
    return await _set_status(request, B.STATUS_FIRED, "❌ Вашу заявку відхилено.")

async def api_fire(request):
    return await _set_status(request, B.STATUS_FIRED, "Ваш доступ призупинено керівником.")

async def api_restore(request):
    return await _set_status(request, B.STATUS_ACTIVE, "♻️ Ваш доступ відновлено.")


# ─── ЕНДПОІНТИ: аналітика ─────────────────────────────────────────────────────

def _parse_dmy(s):
    try:
        return B.datetime.strptime(str(s)[:10], "%d.%m.%Y")
    except Exception:
        return None


async def api_analytics(request):
    uid = request["uid"]
    period = request.query.get("period", "7d")
    days = {"today": 1, "7d": 7, "30d": 30}.get(period, 7)
    now = B.datetime.now()
    cutoff = now - B.timedelta(days=days)
    open_count = 0
    closed_in = 0
    new_in = 0
    by_venue = {}
    by_category = {}
    by_urgency = {}
    close_days = []
    workload = {}
    oldest = []
    for fid, rec in B.tasks_store.items():
        r = rec.get("restaurant", "")
        if not _can_view(uid, r):
            continue
        resolved = B._is_resolved(rec.get("status", ""))
        bv = by_venue.setdefault(r or "—", {"open": 0, "closed": 0})
        created = _parse_dmy(rec.get("created_date"))
        if created and created >= cutoff:
            new_in += 1
        if resolved:
            cd = _parse_dmy(rec.get("completed_date"))
            if cd and cd >= cutoff:
                closed_in += 1
                bv["closed"] += 1
                if created:
                    close_days.append(max(0, (cd - created).days))
        else:
            open_count += 1
            bv["open"] += 1
            by_category[rec.get("category", "—")] = by_category.get(rec.get("category", "—"), 0) + 1
            by_urgency[rec.get("urgency", "—")] = by_urgency.get(rec.get("urgency", "—"), 0) + 1
            assign = B.assign_store.get(fid)
            if isinstance(assign, dict) and assign.get("assignee_name"):
                workload[assign["assignee_name"]] = workload.get(assign["assignee_name"], 0) + 1
            oldest.append({"fid": fid, "summary": rec.get("summary", "—"), "restaurant": r,
                           "age_days": int(B._task_age_hours(rec) // 24),
                           "urgency": rec.get("urgency", "—")})
    oldest.sort(key=lambda x: -x["age_days"])
    avg_close = round(sum(close_days) / len(close_days), 1) if close_days else None
    return web.json_response({
        "period": period,
        "open": open_count, "closed_in_period": closed_in, "new_in_period": new_in,
        "avg_days_to_close": avg_close,
        "by_venue": by_venue, "by_category": by_category, "by_urgency": by_urgency,
        "workload": workload, "oldest_open": oldest[:8],
    })


# ─── ЕНДПОІНТИ: підсумки змін ─────────────────────────────────────────────────

def _report_token(key):
    """Непрозорий токен замість ключа звіту `telegram_id:день`. Ключ містить telegram_id,
    тож віддавати його у JSON = де-анонімізація (менеджер крос-референсить /api/staff).
    У чаті цей же ключ лежить лише в callback_data (невидимий). Токен — HMAC токеном бота."""
    return hmac.new(B.BOT_TOKEN.encode(), str(key).encode(), hashlib.sha256).hexdigest()

def _resolve_report_token(token):
    """Зворотне відображення token -> справжній ключ (перебір — звітів небагато)."""
    for key in list(B.reports_store.keys()):
        if hmac.compare_digest(_report_token(key), token):
            return key
    return None


async def api_shift_reports(request):
    uid = request["uid"]
    today = B.datetime.now().strftime("%Y-%m-%d")
    yday = (B.datetime.now() - B.timedelta(days=1)).strftime("%Y-%m-%d")
    day = request.query.get("day")
    if not day:
        day = today if B._shift_reports_for_day(today) else yday
    reports = B._shift_reports_for_day(day)
    summary = ""
    if reports:
        try:
            summary = await B._render_shift_summary(day, reports)
        except Exception as e:
            logger.error(f"shift summary failed: {e}")
            summary = B._fallback_shift_summary(reports)
    out = []
    for r in reports:
        real_key = f"{r.get('telegram_id')}:{r.get('business_day')}"
        out.append({
            "key": _report_token(real_key),   # непрозорий токен (не telegram_id)
            "restaurant": r.get("restaurant", "—"),
            "who": "анонім" if r.get("is_anonymous") else (r.get("sender_name") or "—"),
            "text": r.get("text", ""),
            "is_anonymous": bool(r.get("is_anonymous")),
        })
    return web.json_response({"day": day, "summary": summary, "reports": out, "count": len(out)})


async def api_report_to_task(request):
    uid = request["uid"]
    key = _resolve_report_token(request.match_info["key"])
    rec = B.reports_store.get(key) if key else None
    if not isinstance(rec, dict) or not rec.get("text"):
        return web.json_response({"error": "not_found"}, status=404)
    anon = bool(rec.get("is_anonymous"))
    profile = {"restaurant": rec.get("restaurant", "Терраса"), "role": rec.get("sender_role") or "—",
               "telegram_id": (None if anon else rec.get("telegram_id"))}
    user_data = {"sender_name": rec.get("sender_name") or "Підсумок зміни",
                 "sender_role": rec.get("sender_role") or "—", "is_anonymous": anon}
    results = await B.analyze_with_claude(rec["text"], profile, is_anonymous=anon)
    _, ids, _ = await B._save_and_notify(results, user_data, profile, None, _api_context())
    return web.json_response({"ok": True, "ids": ids})


# ─── СТАТИКА / СЕРВЕР ─────────────────────────────────────────────────────────

def _asset_version():
    """Версія ассетів = найбільший mtime app.js/style.css. Змінюється лише при правці
    файлів -> URL міняється -> Telegram перетягує свіже (анти-кеш Mini App)."""
    try:
        return str(int(max(
            os.path.getmtime(os.path.join(WEBAPP_DIR, "app.js")),
            os.path.getmtime(os.path.join(WEBAPP_DIR, "style.css")))))
    except Exception:
        return "1"


async def index(request):
    # index — завжди свіжий (no-store); до ассетів додаємо ?v=<mtime> для надійного оновлення.
    try:
        with open(os.path.join(WEBAPP_DIR, "index.html"), encoding="utf-8") as f:
            html = f.read()
        v = _asset_version()
        html = (html.replace("/static/app.js", f"/static/app.js?v={v}")
                    .replace("/static/style.css", f"/static/style.css?v={v}"))
        return web.Response(text=html, content_type="text/html",
                            headers={"Cache-Control": "no-store"})
    except Exception:
        return web.FileResponse(os.path.join(WEBAPP_DIR, "index.html"))


async def health(request):
    return web.json_response({"ok": True})


def build_app():
    app = web.Application(middlewares=[error_mw, auth_mw])
    app.router.add_get("/", index)
    app.router.add_get("/health", health)
    app.router.add_get("/api/me", api_me)
    app.router.add_get("/api/tasks", api_tasks)
    app.router.add_post("/api/tasks", api_create_task)
    app.router.add_get("/api/tasks/{fid}", api_task_detail)
    app.router.add_get("/api/photo/{fid}", api_photo)
    app.router.add_post("/api/tasks/{fid}/status", api_status)
    app.router.add_post("/api/tasks/{fid}/delete", api_delete)
    app.router.add_post("/api/tasks/{fid}/comment", api_comment)
    app.router.add_post("/api/tasks/{fid}/assign", api_assign)
    app.router.add_get("/api/staff", api_staff)
    app.router.add_post("/api/staff/{tid}/venues", api_set_venues)
    app.router.add_post("/api/staff/{tid}/role", api_set_role)
    app.router.add_post("/api/staff/{tid}/approve", api_approve)
    app.router.add_post("/api/staff/{tid}/reject", api_reject)
    app.router.add_post("/api/staff/{tid}/fire", api_fire)
    app.router.add_post("/api/staff/{tid}/restore", api_restore)
    app.router.add_get("/api/analytics", api_analytics)
    app.router.add_get("/api/shift-reports", api_shift_reports)
    app.router.add_post("/api/shift-reports/{key}/to-task", api_report_to_task)
    if os.path.isdir(WEBAPP_DIR):
        app.router.add_static("/static/", WEBAPP_DIR)
    return app


async def start(host="127.0.0.1", port=8081):
    """Стартує aiohttp-сервер у поточному (спільному з ботом) event-loop."""
    global _runner
    app = build_app()
    _runner = web.AppRunner(app)
    await _runner.setup()
    site = web.TCPSite(_runner, host, port)
    await site.start()
    logger.info(f"webapp_api: Пульт керівника слухає http://{host}:{port}")
    return _runner


async def stop():
    global _runner
    if _runner is not None:
        try:
            await _runner.cleanup()
        except Exception as e:
            logger.error(f"webapp_api stop error: {e}")
        _runner = None
