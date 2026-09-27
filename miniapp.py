"""
Мини-приложение Telegram для бота Виктории.

Небольшой веб-сервер рядом с ботом: отдаёт страницу miniapp.html и API,
которое читает и меняет те же файлы, что и бот (/data/students.json,
settings.json). Одна база данных — источник правды для чата и приложения.

Подключение в bot.py — три строки в конце функции on_startup() (уже есть):

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
    """Проверяет подпись initData от Telegram WebApp (см. core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app).
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

    return web.json_response({
        "is_admin": False,
        "registered": True,
        "name": s["name"],
        "mode": s.get("mode"),
        "group": s.get("group"),
        "group_label": core.GROUPS.get(s.get("group"), s.get("group")),
        "price": s.get("price"),
        "balance": s.get("balance"),
        "active": s.get("active", True),
        "paused": s.get("paused", False),
        "awaiting_confirmation": s.get("awaiting_confirmation", False),
        "pending_lessons": s.get("pending_lessons"),
        "card_number": core.get_card_number(),
    })


async def handle_students(request):
    user = _auth(request)
    if not user or not core.is_admin(user["id"]):
        return _forbidden()
    db = core.load_db()
    out = [{"id": uid, **s} for uid, s in db.items()]
    return web.json_response({"students": out, "groups": core.GROUPS})


async def handle_paid(request):
    """Ученик жмёт «Я оплатил(а)» из приложения — та же логика, что в боте."""
    user = _auth(request)
    if not user:
        return _unauthorized()
    uid = str(user["id"])
    db = core.load_db()
    s = db.get(uid)
    if not s:
        return web.json_response({"error": "not_registered"}, status=404)

    s["awaiting_confirmation"] = True
    core.save_db(db)

    if s.get("pending_lessons"):
        lessons = s["pending_lessons"]
        price = s.get("price") or 0
        amount = lessons * price
        text = (f"🥰 {s['name']} говорит, что оплатил(а) {lessons} "
                f"{core.lessons_word(lessons)} ({amount}₽) — в приложении.\n\n"
                f"Подтверди в приложении или в чате боту.")
    else:
        text = f"🥰 {s['name']} отметил(а) оплату в приложении (без напоминания). Подтверди сумму в приложении."

    try:
        await core.bot.send_message(core.ADMIN_ID, text)
    except Exception as e:
        logging.error(f"miniapp notify admin error: {e}")

    return web.json_response({"ok": True})


async def handle_confirm(request):
    user = _auth(request)
    if not user or not core.is_admin(user["id"]):
        return _forbidden()
    body = await request.json()
    uid = body.get("uid")
    amount = body.get("amount")  # нужно только для оплаты без предварительного напоминания

    db = core.load_db()
    s = db.get(uid)
    if not s:
        return web.json_response({"error": "not_found"}, status=404)

    if s.get("pending_lessons"):
        lessons = s["pending_lessons"]
    elif amount is not None and s.get("price"):
        lessons = int(amount) // s["price"]
    else:
        return web.json_response({"error": "amount_required"}, status=400)

    s["balance"] = (s.get("balance") or 0) + lessons
    s["pending_lessons"] = None
    s["reminded_at"] = None
    s["followup_sent"] = False
    s["awaiting_confirmation"] = False
    core.save_db(db)

    try:
        await core.bot.send_message(int(uid), "Спасибо за оплату!")
    except Exception as e:
        logging.error(f"miniapp notify student error: {e}")

    return web.json_response({"ok": True, "balance": s["balance"], "lessons": lessons})


async def handle_setup(request):
    user = _auth(request)
    if not user or not core.is_admin(user["id"]):
        return _forbidden()
    body = await request.json()
    uid = body.get("uid")
    price = body.get("price")
    balance = body.get("balance")

    db = core.load_db()
    s = db.get(uid)
    if not s:
        return web.json_response({"error": "not_found"}, status=404)

    if price is not None:
        s["price"] = int(price)
    if balance is not None:
        s["balance"] = int(balance)
    core.save_db(db)

    try:
        await core.bot.send_message(
            int(uid),
            f"Твой абонемент настроен Викторией:\nЦена урока: {s['price']}₽\n"
            f"Баланс: {s['balance']} {core.lessons_word(s['balance'])}"
        )
    except Exception as e:
        logging.error(f"miniapp notify student error: {e}")

    return web.json_response({"ok": True, "student": s})


async def handle_lesson(request):
    user = _auth(request)
    if not user or not core.is_admin(user["id"]):
        return _forbidden()
    body = await request.json()
    kind = body.get("kind")  # "individual" | "group"

    db = core.load_db()
    notified = []

    if kind == "individual":
        uid = body.get("uid")
        s = db.get(uid)
        if not s:
            return web.json_response({"error": "not_found"}, status=404)
        s["balance"] = (s.get("balance") or 0) - 1
        notified.append((uid, s))
    else:
        group_key = body.get("group")
        for uid, s in db.items():
            if s.get("active") and not s.get("paused") and s.get("group") == group_key:
                s["balance"] = (s.get("balance") or 0) - 1
                notified.append((uid, s))

    core.save_db(db)
    for uid, s in notified:
        try:
            await core.bot.send_message(
                int(uid), f"Занятие отмечено. Баланс: {s['balance']} {core.lessons_word(s['balance'])}"
            )
        except Exception as e:
            logging.error(f"miniapp notify student error: {e}")

    return web.json_response({"ok": True, "updated": [{"id": uid, "balance": s["balance"]} for uid, s in notified]})


async def handle_remind(request):
    user = _auth(request)
    if not user or not core.is_admin(user["id"]):
        return _forbidden()
    body = await request.json()
    uid = body.get("uid")
    try:
        lessons = int(body.get("lessons", 0))
    except (TypeError, ValueError):
        lessons = 0

    db = core.load_db()
    s = db.get(uid)
    if not s or not s.get("price") or lessons <= 0:
        return web.json_response({"error": "invalid"}, status=400)

    price = s["price"]
    amount = lessons * price
    s["pending_lessons"] = lessons
    s["reminded_at"] = str(core.date.today())
    s["followup_sent"] = False
    core.save_db(db)

    card = core.get_card_number()
    try:
        await core.bot.send_message(
            int(uid),
            f"{s['name']}, добрый день! Напоминаем об оплате: {lessons} {core.lessons_word(lessons)} — {amount}₽.\n\n"
            f"Оплатить можно на карту: {card}\n\nПосле оплаты нажми кнопку в приложении или в чате🤓"
        )
    except Exception as e:
        logging.error(f"miniapp remind send error: {e}")

    return web.json_response({"ok": True, "amount": amount})


async def handle_manage(request):
    """action: toggle_pause | remove | reactivate"""
    user = _auth(request)
    if not user or not core.is_admin(user["id"]):
        return _forbidden()
    body = await request.json()
    uid = body.get("uid")
    action = body.get("action")

    db = core.load_db()
    s = db.get(uid)
    if not s:
        return web.json_response({"error": "not_found"}, status=404)

    if action == "toggle_pause":
        s["paused"] = not s.get("paused", False)
    elif action == "remove":
        s["active"] = False
    elif action == "reactivate":
        s["active"] = True
        s["paused"] = False
        s["reminded_at"] = None
        s["pending_lessons"] = None
        s["followup_sent"] = False
        s["awaiting_confirmation"] = False
    else:
        return web.json_response({"error": "unknown_action"}, status=400)

    core.save_db(db)
    return web.json_response({"ok": True, "student": s})


async def start_miniapp(core_module):
    global core
    core = core_module

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

    runner = web.AppRunner(app)
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
