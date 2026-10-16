"""Тести /password (iiko_password.py) без Telegram і без мережі: фейкові update/context, підмінений _post.

Що стереже:
  A. Номер: той самий розбір, що в iiko-ap (normPhoneUA) — однакові записи дають однаковий результат.
  B. Контакт: лише СВІЙ (contact.user_id == від кого), лише після /password і не пізніше 5 хв, лише активний профіль,
     номер профілю мусить збігатися; едж кличеться з нормалізованим номером і telegram_id.
  C. Посилання: шлеться без клавіатури й прибирається з чату за 15 хв; контакт (номер) прибирається з чату;
     після відповіді основна клавіатура повертається ПОСТІЙНИМ повідомленням.
  D. Відмови еджа → людський текст, а не посилання; падіння мережі → «тимчасово недоступний».
  E. ПРОБА НА ЧЕРВОНЕ (--mutate): мутанти модуля — кожен мусить завалити набір.

    venv/bin/python test_iiko_password.py [--mutate]
"""
import asyncio
import importlib.util
import os
import sys
import tempfile
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "iiko_password.py")


class Msg:
    def __init__(self, chat_id, mid, text=None, contact=None):
        self.chat_id, self.message_id, self.text, self.contact = chat_id, mid, text, contact


def make_env(profiles):
    sent, deleted = [], []

    async def safe_send_message(bot, chat_id, text, reply_markup=None, parse_mode="Markdown"):
        m = Msg(chat_id, 1000 + len(sent), text)
        sent.append({"chat": chat_id, "text": text, "kb": reply_markup, "mid": m.message_id})
        return m

    def schedule_delete(context, bot, chat_id, message_id, delay, job_name=None):
        deleted.append((message_id, delay))

    B = types.SimpleNamespace(
        user_profiles=profiles, STATUS_ACTIVE="active", get_bot=lambda: object(),
        safe_send_message=safe_send_message, schedule_delete=schedule_delete,
        get_main_keyboard=lambda uid: "MAIN_KB",
        logger=types.SimpleNamespace(info=lambda *a: None, error=lambda *a: None),
    )
    return B, sent, deleted


def upd(uid, chat_type="private", text=None, contact=None, mid=55):
    return types.SimpleNamespace(
        effective_user=types.SimpleNamespace(id=uid),
        effective_chat=types.SimpleNamespace(id=uid, type=chat_type),
        message=Msg(uid, mid, text=text, contact=contact),
    )


def contact(user_id, phone):
    return types.SimpleNamespace(user_id=user_id, phone_number=phone)


def suite(M):
    fails = []

    def eq(a, b, m):
        if a != b:
            fails.append(f"{m}: {a!r} != {b!r}")

    # A — ті самі записи, що в iiko-ap scripts/staff_login_test.mjs
    eq(M.norm_phone_ua("+380 67 431 58 92"), "+380674315892", "A1")
    eq(M.norm_phone_ua("380504127736"), "+380504127736", "A2")
    eq(M.norm_phone_ua("0931862047"), "+380931862047", "A3")
    eq(M.norm_phone_ua("(096) 275-14-83"), "+380962751483", "A4")
    eq(M.norm_phone_ua("661739025"), "+380661739025", "A5")
    eq(M.norm_phone_ua("8 073 518 64 09"), "+380735186409", "A6")
    eq(M.norm_phone_ua("+48 512 345 678"), None, "A7")
    eq(M.norm_phone_ua("+661739025"), None, "A8")
    eq(M.norm_phone_ua("0012345678"), None, "A9")
    eq(M.norm_phone_ua("0931862047x"), None, "A10")
    eq(M.norm_phone_ua(None), None, "A11")

    UID, OTHER = 7311, 8422
    profiles = {
        UID: {"status": "active", "phone": "067 431 58 92"},
        OTHER: {"status": "pending", "phone": "0504127736"},
        9533: {"status": "active"},  # без номера в профілі
    }

    def run(scenario):
        B, sent, deleted = make_env(profiles)
        M.init(B)
        calls = []

        async def fake_post(payload):
            calls.append(payload)
            return scenario.get("edge", (200, {"ok": True, "url": "https://x/set-password#t=TOK", "ttl_min": 15}))

        if scenario.get("edge") == "boom":
            async def fake_post(payload):  # noqa: F811
                calls.append(payload)
                raise RuntimeError("network")
        M._post = fake_post
        ctx = types.SimpleNamespace(user_data={})
        loop = asyncio.new_event_loop()
        try:
            for step in scenario["steps"]:
                kind = step[0]
                if kind == "cmd":
                    loop.run_until_complete(M.cmd_password(upd(step[1], chat_type=step[2] if len(step) > 2 else "private"), ctx))
                elif kind == "contact":
                    r = loop.run_until_complete(M.on_contact(upd(step[1], contact=step[2], mid=77), ctx))
                    scenario.setdefault("ret", []).append(r)
                elif kind == "age":
                    ctx.user_data.get(M.STATE_KEY, {})["ts"] = time.time() - step[1]
                elif kind == "cancel":
                    r = loop.run_until_complete(M.on_cancel(upd(step[1], text=M.CANCEL_BTN, mid=66), ctx))
                    scenario.setdefault("ret", []).append(r)
        finally:
            loop.close()
        return sent, deleted, calls, scenario.get("ret", [])

    # B1 щасливий шлях
    sent, deleted, calls, ret = run({"steps": [("cmd", UID), ("contact", UID, contact(UID, "+380674315892"))]})
    eq(calls, [{"action": "issue", "phone": "+380674315892", "telegram_id": UID}], "B1 едж з нормалізованим номером")
    link = [s for s in sent if "set-password" in s["text"]]
    eq(len(link), 1, "C1 посилання надіслано")
    if link:
        eq(link[0]["kb"], None, "C2 посилання без клавіатури")
        eq((link[0]["mid"], 15 * 60) in deleted, True, "C3 посилання зникає за 15 хв")
    eq((77, 5) in deleted, True, "C4 контакт прибирається з чату")
    eq(sent[-1]["kb"], "MAIN_KB", "C5 основна клавіатура — на останньому повідомленні")
    eq(any(mid == sent[-1]["mid"] for mid, _ in deleted), False, "C6 повідомлення з клавіатурою не автовидаляється")
    first = sent[0]
    eq(getattr(getattr(first["kb"], "keyboard", [[None]])[0][0], "request_contact", None), True, "C7 кнопка просить контакт")

    # B2 чужий контакт
    sent, deleted, calls, ret = run({"steps": [("cmd", UID), ("contact", UID, contact(OTHER, "+380674315892"))]})
    eq(calls, [], "B2 чужий контакт — едж не кличеться")
    eq(any("set-password" in s["text"] for s in sent), False, "B2 чужий контакт — без посилання")
    # B3 контакт без user_id (пересланий з адресної книги)
    sent, deleted, calls, ret = run({"steps": [("cmd", UID), ("contact", UID, contact(None, "+380674315892"))]})
    eq(calls, [], "B3 контакт без user_id — едж не кличеться")
    # B4 без /password контакт не наш
    sent, deleted, calls, ret = run({"steps": [("contact", UID, contact(UID, "+380674315892"))]})
    eq((calls, ret), ([], [False]), "B4 контакт без /password ігнорується")
    # B5 прострочено
    sent, deleted, calls, ret = run({"steps": [("cmd", UID), ("age", 301), ("contact", UID, contact(UID, "+380674315892"))]})
    eq((calls, ret), ([], [False]), "B5 через 5 хв — ігнорується")
    # B6 неактивний профіль
    sent, deleted, calls, ret = run({"steps": [("cmd", OTHER), ("contact", OTHER, contact(OTHER, "+380504127736"))]})
    eq(calls, [], "B6 неактивний — едж не кличеться")
    eq(any("лише для активних" in s["text"] for s in sent), True, "B6 неактивному пояснено")
    # B7 номер профілю інший
    sent, deleted, calls, ret = run({"steps": [("cmd", UID), ("contact", UID, contact(UID, "+380931862047"))]})
    eq(calls, [], "B7 номер ≠ профілю — едж не кличеться")
    # B8 профіль без номера — довіряємо контакту Telegram
    sent, deleted, calls, ret = run({"steps": [("cmd", 9533), ("contact", 9533, contact(9533, "380661739025"))]})
    eq([c["phone"] for c in calls], ["+380661739025"], "B8 профіль без номера — номер з контакту")
    # B9 група — нічого
    sent, deleted, calls, ret = run({"steps": [("cmd", UID, "group"), ("contact", UID, contact(UID, "+380674315892"))]})
    eq(calls, [], "B9 у групі команда не працює")
    # B10 повторний контакт після використаного стану
    sent, deleted, calls, ret = run({"steps": [("cmd", UID), ("contact", UID, contact(UID, "+380674315892")), ("contact", UID, contact(UID, "+380674315892"))]})
    eq(len(calls), 1, "B10 один /password — одне посилання")
    # D відмови
    sent, deleted, calls, ret = run({"steps": [("cmd", UID), ("contact", UID, contact(UID, "+380674315892"))], "edge": (200, {"ok": False, "reason": "not_allowed"})})
    eq(any("не в списку" in s["text"] for s in sent) and not any("set-password" in s["text"] for s in sent), True, "D1 not_allowed")
    sent, deleted, calls, ret = run({"steps": [("cmd", UID), ("contact", UID, contact(UID, "+380674315892"))], "edge": (429, {"ok": False, "reason": "rate_limited"})})
    eq(any("Забагато" in s["text"] for s in sent), True, "D2 ліміт")
    sent, deleted, calls, ret = run({"steps": [("cmd", UID), ("contact", UID, contact(UID, "+380674315892"))], "edge": "boom"})
    eq(any("тимчасово недоступний" in s["text"] for s in sent), True, "D3 мережа впала")
    sent, deleted, calls, ret = run({"steps": [("cmd", UID), ("contact", UID, contact(UID, "+380674315892"))], "edge": (200, {"ok": True})})
    eq(any("тимчасово" in s["text"] for s in sent), True, "D4 ok без url — не вдаємо успіх")
    # cancel
    sent, deleted, calls, ret = run({"steps": [("cmd", UID), ("cancel", UID), ("contact", UID, contact(UID, "+380674315892"))]})
    eq((calls, ret[0]), ([], True), "B11 «Не зараз» скасовує")
    return fails


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def main():
    total = 0
    for f in suite(load(SRC, "iiko_password_base")):
        total += 1
        print("❌", f)
    if "--mutate" in sys.argv:
        code = open(SRC, encoding="utf8").read()
        MUT = [
            ("чужий контакт", "if contact is None or contact.user_id != uid:", "if contact is None:"),
            ("контакт без user_id", "if contact is None or contact.user_id != uid:", "if contact is None or (contact.user_id and contact.user_id != uid):"),
            ("номер профілю не звіряється", "if prof_phone and prof_phone != phone:", "if False:"),
            ("неактивний", 'return B.user_profiles.get(user_id, {}).get("status") == B.STATUS_ACTIVE', "return True"),
            ("без строку стану", 'if not st or time.time() - st.get("ts", 0) > STATE_TTL:', "if not st:"),
            ("стан не гаситься", "st = context.user_data.pop(STATE_KEY, None)", "st = context.user_data.get(STATE_KEY)"),
            ("у групі", 'return update.effective_chat is not None and update.effective_chat.type == "private"', "return True"),
            ("сирий номер в едж", '{"action": "issue", "phone": phone, "telegram_id": uid}', '{"action": "issue", "phone": contact.phone_number, "telegram_id": uid}'),
            ("посилання вічне", "delete_after=LINK_DELETE_AFTER)", "delete_after=None)"),
            ("контакт лишається в чаті", "B.schedule_delete(context, bot, update.effective_chat.id, update.message.message_id, delay=5)\n    if not _active(uid):", "if not _active(uid):"),
            ("ok без url", 'if j.get("ok") and j.get("url"):', 'if j.get("ok"):'),
            ("8-ка", 'elif len(d) == 11 and d.startswith("80"):', 'elif False:'),
            ("нуль після коду", 'if not n or len(n) != 9 or n.startswith("0"):', "if not n or len(n) != 9:"),
            ("літери", 'if not s or re.search(r"[^\\d\\s+()\\-.]", s):', "if not s:"),
            ("клавіатура на посиланні", "delete_after=LINK_DELETE_AFTER)\n        await _restore_keyboard", "keyboard=B.get_main_keyboard(uid), delete_after=LINK_DELETE_AFTER)\n        await _restore_keyboard"),
            ("скасування не гасить", "if not _private(update) or not context.user_data.pop(STATE_KEY, None):", "if not _private(update) or not context.user_data.get(STATE_KEY):"),
        ]
        killed = 0
        d = tempfile.mkdtemp(prefix="iikopwmut-")
        for i, (name, a, b) in enumerate(MUT):
            if a not in code:
                total += 1
                print(f"❌ мутант «{name}»: шаблон не знайдено")
                continue
            p = os.path.join(d, f"m{i}.py")
            open(p, "w", encoding="utf8").write(code.replace(a, b, 1))
            try:
                bad = len(suite(load(p, f"mut{i}")))
            except Exception:
                bad = 1
            if bad:
                killed += 1
            else:
                total += 1
                print(f"❌ мутант «{name}» ВИЖИВ")
        print(f"мутантів: {len(MUT)}, убито {killed}")
    print("\n❌ %d збоїв" % total if total else "\n✅ усе зелене")
    sys.exit(1 if total else 0)


if __name__ == "__main__":
    main()
