"""
Мини-приложение Telegram для бота Виктории.

Небольшой веб-сервер рядом с ботом: отдаёт страницу miniapp.html и API,
которое читает и меняет те же файлы, что и бот (/data/students.json,
settings.json). Одна база данных — источник правды для чата и приложения.

Подключение в bot.py — в конце on_startup() (уже есть):

    import sys
    from miniapp import start_miniapp
    await start_miniapp(sys.modules[__name__])

Переменные окружения (Railway → Variables):
    WEBAPP_URL — адрес сервиса, например https://victoria-bot.up.railway.app
    PORT       — Railway задаёт сам
"""
import os
import hmac
import json
import hashlib
import logging
from urllib.parse import parse_qsl

from aiohttp import web
from aiogram.types import MenuButtonWebApp, WebAppInfo

core = None  # модуль бота (bot.py), передаётся в start_miniapp
HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "miniapp.html")


# ---------- проверка, что запрос действительно пришёл из Telegram ----------
def check_init_data(init_data: str, bot_token: str):
    """Проверяет подпись initData от Telegram WebApp
    (core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app).
    Возвращает dict с данными пользователя или None, если подпись неверна."""
    if not init_data or not bot_token:
        return None
    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated_hash, received_hash):
        return None
    user_json = pairs.get("user")
    if not user_json:
        return None
    try:
        return json.loads(user_json)
    except json.JSONDecodeError:
        return None


def _auth(request):
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    return check_init_data(init_data, core.TOKEN)


def _forbidden():
    return web.json_response({"error": "forbidden"}, status=403)

def _unauthorized():
    return web.json_response({"error": "unauthorized"}, status=401)

def _bad(error, status=400):
    return web.json_response({"error": error}, status=status)

async def _json(request):
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

async def _notify(uid, text, **kwargs):
    try:
        await core.bot.send_message(int(uid), text, reply_markup=core.student_menu(), **kwargs)
    except Exception as e:
        logging.error(f"miniapp notify student error: {e}")


# ---------- страницы и API ----------
async def handle_index(request):
    return web.FileResponse(HTML_PATH)


async def handle_me(request):
    user = _auth(request)
    if not user:
        return _unauthorized()

    if core.is_admin(user["id"]):
        return web.json_response({"is_admin": True, "name": user.get("first_name", "")})

    s = core.get_student(user["id"])
    if not s:
        return web.json_response({"is_admin": False, "registered": False})

    enrollments = []
    for key in core.sorted_enroll_keys(s):
        e = core.get_enrollments(s)[key]
        enrollments.append({
            "key": key,
            "label": core.enroll_label(key),
            "price": e.get("price"),
            "balance": e.get("balance"),
            "pending_lessons": e.get("pending_lessons"),
            "awaiting_confirmation": e.get("awaiting_confirmation", False),
        })

    return web.json_response({
        "is_admin": False,
        "registered": True,
        "name": s["name"],
        "active": s.get("active", True),
        "paused": s.get("paused", False),
        "enrollments": enrollments,
        "card_number": core.get_card_number(),
    })


async def handle_students(request):
    user = _auth(request)
    if not user or not core.is_admin(user["id"]):
        return _forbidden()
    db = core.load_db()
    students = []
    for uid, s in db.items():
        students.append({
            "id": uid,
            "name": s.get("name", ""),
            "active": s.get("active", True),
            "paused": s.get("paused", False),
            "enrollments": core.get_enrollments(s),
        })
    keys = core.all_enroll_keys()
    return web.json_response({
        "students": students,
        "keys": keys,
        "labels": {k: core.enroll_label(k) for k in keys},
    })


async def handle_paid(request):
    """Ученик жмёт «Я оплатил(а)» из приложения.
    body: {key?, lessons?} — key нужен, если у ученика больше одной подписки;
    lessons нужен, если по этой подписке нет активного напоминания."""
    user = _auth(request)
    if not user:
        return _unauthorized()
    uid = str(user["id"])
    db = core.load_db()
    s = db.get(uid)
    if not s:
        return _bad("not_registered", 404)

    body = await _json(request)
    enr = core.get_enrollments(s)
    key = body.get("key")
    if not key:
        if len(enr) == 1:
            key = next(iter(enr))
        else:
            return _bad("key_required")
    e = enr.get(key)
    if e is None:
        return _bad("unknown_enrollment", 404)

    if e.get("pending_lessons"):
        lessons = e["pending_lessons"]
        had_reminder = True
    else:
        try:
            lessons = int(body.get("lessons"))
        except (TypeError, ValueError):
            return _bad("lessons_required")
        if lessons <= 0:
            return _bad("invalid_lessons")
        e["pending_lessons"] = lessons
        had_reminder = False

    amount = lessons * (e.get("price") or 0)
    e["awaiting_confirmation"] = True
    core.save_db(db)

    suffix = "" if had_reminder else " — без предварительного напоминания"
    text = (f"🥰 {s['name']} говорит, что оплатил(а){core.enroll_for(s, key)} {lessons} "
            f"{core.lessons_word(lessons)} ({amount}₽){suffix} — в приложении.\n\n"
            f"Подтверди в приложении или в чате боту.")
    try:
        await core.bot.send_message(core.ADMIN_ID, text)
    except Exception as ex:
        logging.error(f"miniapp notify admin error: {ex}")

    return web.json_response({"ok": True})


async def handle_confirm(request):
    user = _auth(request)
    if not user or not core.is_admin(user["id"]):
        return _forbidden()
    body = await _json(request)
    uid, key = body.get("uid"), body.get("key")

    db = core.load_db()
    s = db.get(uid)
    e = core.get_enrollments(s or {}).get(key)
    if not s or e is None:
        return _bad("not_found", 404)
    if not e.get("pending_lessons"):
        return _bad("nothing_to_confirm")

    lessons = e["pending_lessons"]
    e["balance"] = (e.get("balance") or 0) + lessons
    core.reset_enrollment_reminder(e)
    core.save_db(db)

    await _notify(uid, "Спасибо за оплату!")
    return web.json_response({"ok": True, "balance": e["balance"], "lessons": lessons})


async def handle_setup(request):
    """Создаёт подписку (если её ещё нет) или меняет цену/баланс существующей.
    Так же работает «добавить ученика в занятия»."""
    user = _auth(request)
    if not user or not core.is_admin(user["id"]):
        return _forbidden()
    body = await _json(request)
    uid, key = body.get("uid"), body.get("key")
    if not core.valid_enroll_key(key):
        return _bad("unknown_enrollment")

    db = core.load_db()
    s = db.get(uid)
    if not s:
        return _bad("not_found", 404)

    try:
        price = None if body.get("price") is None else int(body["price"])
        balance = None if body.get("balance") is None else int(body["balance"])
    except (TypeError, ValueError):
        return _bad("invalid_number")

    e = s.setdefault("enrollments", {}).setdefault(key, core.new_enrollment())
    if price is not None:
        e["price"] = price
    if balance is not None:
        e["balance"] = balance
    core.save_db(db)

    if core.is_configured(e):
        await _notify(
            uid,
            f"Твой абонемент настроен Викторией — {core.enroll_label(key)}:\n"
            f"Цена урока: {e['price']}₽\nБаланс: {e['balance']} {core.lessons_word(e['balance'])}"
        )
    return web.json_response({"ok": True})


async def handle_lesson(request):
    user = _auth(request)
    if not user or not core.is_admin(user["id"]):
        return _forbidden()
    body = await _json(request)
    kind = body.get("kind")  # "individual" | "group"

    db = core.load_db()
    notified = []

    if kind == "individual":
        uid = body.get("uid")
        s = db.get(uid)
        e = core.get_enrollments(s or {}).get(core.INDIVIDUAL)
        if not s or e is None:
            return _bad("not_found", 404)
        e["balance"] = (e.get("balance") or 0) - 1
        notified.append((uid, s, core.INDIVIDUAL, e))
    elif kind == "group":
        group_key = body.get("group")
        if group_key not in core.GROUPS:
            return _bad("unknown_group")
        for uid, s in db.items():
            e = core.get_enrollments(s).get(group_key)
            if e is not None and s.get("active") and not s.get("paused"):
                e["balance"] = (e.get("balance") or 0) - 1
                notified.append((uid, s, group_key, e))
    else:
        return _bad("unknown_kind")

    core.save_db(db)
    for uid, s, key, e in notified:
        await _notify(
            uid,
            f"Занятие отмечено{core.enroll_tag(s, key)}. Баланс: {e['balance']} {core.lessons_word(e['balance'])}"
        )
    return web.json_response({"ok": True, "updated": [{"id": uid, "balance": e["balance"]} for uid, s, key, e in notified]})


async def handle_remind(request):
    user = _auth(request)
    if not user or not core.is_admin(user["id"]):
        return _forbidden()
    body = await _json(request)
    uid, key = body.get("uid"), body.get("key")
    try:
        lessons = int(body.get("lessons", 0))
    except (TypeError, ValueError):
        lessons = 0

    db = core.load_db()
    s = db.get(uid)
    e = core.get_enrollments(s or {}).get(key)
    if not s or e is None or e.get("price") is None or lessons <= 0:
        return _bad("invalid")

    amount = lessons * e["price"]
    e["pending_lessons"] = lessons
    e["reminded_at"] = str(core.date.today())
    e["followup_sent"] = False
    e["awaiting_confirmation"] = False
    core.save_db(db)

    await _notify(uid, core.build_reminder_text(s, key, lessons), parse_mode="HTML")
    return web.json_response({"ok": True, "amount": amount})


async def handle_manage(request):
    """action: toggle_pause | remove | reactivate (на уровне ученика целиком)"""
    user = _auth(request)
    if not user or not core.is_admin(user["id"]):
        return _forbidden()
    body = await _json(request)
    uid, action = body.get("uid"), body.get("action")

    db = core.load_db()
    s = db.get(uid)
    if not s:
        return _bad("not_found", 404)

    if action == "toggle_pause":
        s["paused"] = not s.get("paused", False)
    elif action == "remove":
        s["active"] = False
    elif action == "reactivate":
        s["active"] = True
        s["paused"] = False
        for e in core.get_enrollments(s).values():
            core.reset_enrollment_reminder(e)
    else:
        return _bad("unknown_action")

    core.save_db(db)
    return web.json_response({"ok": True})


def build_app():
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/me", handle_me)
    app.router.add_get("/api/students", handle_students)
    app.router.add_post("/api/paid", handle_paid)
    app.router.add_post("/api/confirm", handle_confirm)
    app.router.add_post("/api/setup", handle_setup)
    app.router.add_post("/api/lesson", handle_lesson)
    app.router.add_post("/api/remind", handle_remind)
    app.router.add_post("/api/manage", handle_manage)
    return app


async def start_miniapp(core_module):
    global core
    core = core_module

    runner = web.AppRunner(build_app())
    await runner.setup()
    port = int(os.getenv("PORT", "8080"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info(f"Mini App запущено на порту {port}")

    webapp_url = os.getenv("WEBAPP_URL")
    if webapp_url:
        try:
            await core.bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(text="Приложение", web_app=WebAppInfo(url=webapp_url))
            )
        except Exception as e:
            logging.error(f"Не удалось установить кнопку меню: {e}")
