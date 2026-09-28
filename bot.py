import os
import json
import logging
from html import escape as h
from datetime import datetime, date, timedelta, timezone
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder
import asyncio

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DB_FILE = "/data/students.json"
SETTINGS_FILE = "/data/settings.json"
DEFAULT_CARD_NUMBER = os.getenv("DEFAULT_CARD_NUMBER", "5536913882470939")
CONTACT_URL = os.getenv("CONTACT_URL", "https://t.me/Victoria_Skypish")

# Пока только одна группа, но структура сразу рассчитана на добавление новых
GROUPS = {
    "wednesday": "Wednesday (17:00)",
}
INDIVIDUAL = "individual"

MSK = timezone(timedelta(hours=3))

def now_msk():
    return datetime.now(MSK)

logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())


# ============================================================
# Хранение данных
#
# У ученика может быть несколько «подписок» (enrollments):
#   "individual" — индивидуальные занятия
#   "<ключ группы>" — занятия в группе (например "wednesday")
# У каждой подписки свои цена урока, баланс, напоминания и подтверждения оплаты.
#
#   {
#     "name": "...", "active": true, "paused": false, "registered_at": "...",
#     "enrollments": {
#        "individual": {"price": 3000, "balance": 2, "pending_lessons": null,
#                       "reminded_at": null, "followup_sent": false,
#                       "awaiting_confirmation": false},
#        "wednesday":  {...}
#     }
#   }
# ============================================================
def load_db():
    if not os.path.exists(DB_FILE):
        return {}
    with open(DB_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_db(db):
    os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)

def get_student(user_id):
    return load_db().get(str(user_id))

def load_settings():
    if not os.path.exists(SETTINGS_FILE):
        return {}
    with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_settings(s):
    os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)

def get_card_number():
    return load_settings().get("card_number") or DEFAULT_CARD_NUMBER

def is_admin(user_id):
    return user_id == ADMIN_ID


# ---------- подписки ----------
def all_enroll_keys():
    return [INDIVIDUAL] + list(GROUPS.keys())

def valid_enroll_key(key):
    return key == INDIVIDUAL or key in GROUPS

def enroll_label(key):
    if key == INDIVIDUAL:
        return "Индивидуальные занятия"
    return GROUPS.get(key, key)

def enroll_short(key):
    return "Инд." if key == INDIVIDUAL else GROUPS.get(key, key)

def new_enrollment(price=None, balance=None):
    return {
        "price": price,
        "balance": balance,
        "pending_lessons": None,
        "reminded_at": None,
        "followup_sent": False,
        "awaiting_confirmation": False,
    }

def get_enrollments(s):
    return s.get("enrollments") or {}

def sorted_enroll_keys(s):
    enr = get_enrollments(s)
    known = [k for k in all_enroll_keys() if k in enr]
    return known + [k for k in enr if k not in known]

def enroll_tag(s, key):
    """« — Название» — только если у ученика больше одной подписки."""
    return f" — {enroll_label(key)}" if len(get_enrollments(s)) > 1 else ""

def enroll_for(s, key):
    """« за «Название»» — только если у ученика больше одной подписки."""
    return f" за «{enroll_label(key)}»" if len(get_enrollments(s)) > 1 else ""

def is_configured(e):
    return e.get("price") is not None and e.get("balance") is not None

def reset_enrollment_reminder(e):
    e["pending_lessons"] = None
    e["reminded_at"] = None
    e["followup_sent"] = False
    e["awaiting_confirmation"] = False


def migrate_students():
    """Разово переводит старые записи (один абонемент прямо в карточке ученика:
    mode/group/price/balance/...) на структуру enrollments. Старые поля
    оставляем как есть — они больше не читаются, но их наличие ничему не мешает."""
    db = load_db()
    changed = False
    for uid, s in db.items():
        if "enrollments" in s:
            continue
        if s.get("mode") == "individual":
            key = INDIVIDUAL
        else:
            key = s.get("group") or next(iter(GROUPS))
        e = new_enrollment(s.get("price"), s.get("balance"))
        e["pending_lessons"] = s.get("pending_lessons")
        e["reminded_at"] = s.get("reminded_at")
        e["followup_sent"] = s.get("followup_sent", False)
        e["awaiting_confirmation"] = s.get("awaiting_confirmation", False)
        s["enrollments"] = {key: e}
        changed = True
    if changed:
        save_db(db)
        logging.info("Миграция учеников на структуру enrollments выполнена.")


# ---------- склонение "урок/урока/уроков" ----------
def ru_plural(n, one, few, many):
    n_abs = abs(n)
    if n_abs % 100 in (11, 12, 13, 14):
        return many
    last_digit = n_abs % 10
    if last_digit == 1:
        return one
    elif 2 <= last_digit <= 4:
        return few
    return many

def lessons_word(n):
    return ru_plural(n, "урок", "урока", "уроков")


# ---------- тексты ----------
def enroll_status_html(e):
    price, balance = e.get("price"), e.get("balance")
    if price is None or balance is None:
        return "⏳ Виктория ещё настраивает этот абонемент"
    if balance > 0:
        bal = f"✅ Осталось: <b>{balance} {lessons_word(balance)}</b>"
    else:
        bal = f"❗ Баланс: <b>{balance} {lessons_word(balance)}</b>"
    return f"💳 Цена урока: {price}₽\n{bal}"

def student_status_html(s, greet=True):
    lines = [f"Привет, {h(s['name'])}!"] if greet else [h(s["name"])]
    for key in sorted_enroll_keys(s):
        e = get_enrollments(s)[key]
        lines.append(f"\n<b>{h(enroll_label(key))}</b>\n{enroll_status_html(e)}")
    return "\n".join(lines)

def build_reminder_text(s, key, lessons, followup=False):
    """Текст напоминания об оплате. Номер карты в <code> — в Telegram
    по нажатию на такой текст он копируется."""
    e = get_enrollments(s)[key]
    amount = lessons * (e.get("price") or 0)
    what = f"{lessons} {lessons_word(lessons)} — {amount}₽"
    for_ = h(enroll_for(s, key))
    name = h(s["name"])
    if followup:
        head = f"{name}, напоминаем: не забудьте оплатить {what}{for_}."
    else:
        head = f"{name}, добрый день! Напоминаем об оплате{for_}: {what}."
    return (
        f"{head}\n\n"
        f"Реквизиты для оплаты (нажми на номер, чтобы скопировать):\n"
        f"<code>{h(get_card_number())}</code>\n\n"
        f"После оплаты нажми кнопку ниже🤓"
    )


# ---------- FSM состояния ----------
class Register(StatesGroup):
    waiting_name = State()

class SetupStudent(StatesGroup):
    waiting_price = State()
    waiting_balance = State()

class SendReminder(StatesGroup):
    waiting_lessons = State()

class StudentReportPayment(StatesGroup):
    waiting_lessons = State()


# ---------- меню ----------
def student_menu():
    b = InlineKeyboardBuilder()
    b.button(text="Я оплатил(а)", callback_data="paid")
    b.button(text="Статус абонемента", callback_data="my_status")
    b.button(text="Написать Виктории", url=CONTACT_URL)
    b.adjust(1)
    return b.as_markup()

def admin_menu():
    b = InlineKeyboardBuilder()
    b.button(text="👥 Все ученики", callback_data="admin_list")
    b.button(text="📚 Провести урок", callback_data="lesson_menu")
    b.button(text="🔔 Напоминание об оплате", callback_data="remind_menu")
    b.button(text="➕ Добавить в занятия", callback_data="enroll_menu")
    b.button(text="⏸ Пауза / удаление", callback_data="manage_menu")
    b.button(text="♻️ Реактивировать", callback_data="reactivate_menu")
    b.adjust(1)
    return b.as_markup()

def mode_labels(s):
    return " + ".join(enroll_label(k) for k in sorted_enroll_keys(s)) or "—"


# ============================================================
# Регистрация
# ============================================================
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    if is_admin(message.from_user.id):
        await message.answer("Панель управления:", reply_markup=admin_menu())
        return

    s = get_student(message.from_user.id)

    if not s:
        await message.answer("Добрый день! Напишите, пожалуйста, своё имя и фамилию🤓")
        await state.set_state(Register.waiting_name)
        return

    if not s.get("active"):
        await message.answer("Ваш аккаунт неактивен. Напишите Виктории для возобновления занятий.")
        return
    if s.get("paused"):
        await message.answer("Ваши занятия сейчас на паузе. Напишите Виктории для возобновления.")
        return

    enr = get_enrollments(s)
    if not any(is_configured(e) for e in enr.values()):
        await message.answer(
            f"Привет, {s['name']}!\n\nВиктория скоро настроит твой абонемент — подожди немного🤓",
            reply_markup=student_menu()
        )
        return

    await message.answer(student_status_html(s), parse_mode="HTML", reply_markup=student_menu())


@dp.message(Register.waiting_name)
async def reg_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    first_group = next(iter(GROUPS))
    b = InlineKeyboardBuilder()
    b.button(text="Индивидуально", callback_data="reg_indiv")
    for key, label in GROUPS.items():
        b.button(text=f"Группа {label}", callback_data=f"reg_group_{key}")
    b.button(text=f"И индивидуально, и в группе {GROUPS[first_group]}", callback_data="reg_both")
    b.adjust(1)
    await message.answer("Ты занимаешься индивидуально, в группе или и так и так?", reply_markup=b.as_markup())


@dp.callback_query(F.data.startswith("reg_"))
async def reg_mode(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    name = data.get("name")
    if not name:
        await callback.answer()
        return

    first_group = next(iter(GROUPS))
    code = callback.data
    if code == "reg_indiv":
        keys = [INDIVIDUAL]
    elif code == "reg_both":
        keys = [INDIVIDUAL, first_group]
    elif code.startswith("reg_group_") and code.replace("reg_group_", "") in GROUPS:
        keys = [code.replace("reg_group_", "")]
    else:
        await callback.answer()
        return

    uid = str(callback.from_user.id)
    student = {
        "name": name,
        "active": True,
        "paused": False,
        "registered_at": str(date.today()),
        "enrollments": {k: new_enrollment() for k in keys},
    }
    db = load_db()
    db[uid] = student
    save_db(db)
    await state.clear()

    labels = " + ".join(enroll_label(k) for k in keys)
    await callback.message.answer(
        f"Спасибо, {name}! Ты зарегистрирован(а): {labels}.\n\nВиктория скоро настроит твой абонемент🤓"
    )

    b = InlineKeyboardBuilder()
    for k in keys:
        b.button(text=f"⚙️ Настроить: {enroll_label(k)}", callback_data=f"setup_{uid}|{k}")
    b.adjust(1)
    await bot.send_message(
        ADMIN_ID,
        f"Новый ученик:\n{name}\n{labels}\nID: {uid}",
        reply_markup=b.as_markup()
    )
    await callback.answer()


@dp.callback_query(F.data == "my_status")
async def my_status(callback: types.CallbackQuery):
    s = get_student(callback.from_user.id)
    if not s:
        await callback.message.answer("Ты не зарегистрирован(а). Напиши /start")
        await callback.answer()
        return
    if not any(is_configured(e) for e in get_enrollments(s).values()):
        await callback.message.answer("Виктория ещё не настроила твой абонемент — скоро будет готово!", reply_markup=student_menu())
        await callback.answer()
        return
    await callback.message.answer(student_status_html(s, greet=False), parse_mode="HTML", reply_markup=student_menu())
    await callback.answer()


# ============================================================
# Оплата (с подтверждением админом)
# ============================================================
async def start_payment_report(message: types.Message, state: FSMContext, uid: str, key: str):
    db = load_db()
    s = db.get(uid)
    e = get_enrollments(s or {}).get(key)
    if not s or e is None:
        await message.answer("Не нашёл такой абонемент. Напиши /start", reply_markup=student_menu())
        return

    if e.get("pending_lessons"):
        lessons = e["pending_lessons"]
        amount = lessons * (e.get("price") or 0)
        e["awaiting_confirmation"] = True
        save_db(db)
        b = InlineKeyboardBuilder()
        b.button(text="✅ Подтвердить оплату", callback_data=f"confirmpaid_{uid}|{key}")
        b.adjust(1)
        await bot.send_message(
            ADMIN_ID,
            f"🥰 {s['name']} говорит, что оплатил(а){enroll_for(s, key)} {lessons} {lessons_word(lessons)} ({amount}₽).\n\n"
            f"Подтверди, чтобы зачесть оплату:",
            reply_markup=b.as_markup()
        )
        await message.answer("Спасибо! Отправляю Виктории на подтверждение.", reply_markup=student_menu())
    else:
        # Оплата без предварительного напоминания — спрашиваем у самого ученика,
        # сколько уроков он оплатил.
        await state.update_data(report_key=key)
        await state.set_state(StudentReportPayment.waiting_lessons)
        await message.answer(f"Сколько уроков ты оплатил(а){enroll_for(s, key)}? Напиши число:")


@dp.callback_query(F.data == "paid")
async def paid_callback(callback: types.CallbackQuery, state: FSMContext):
    uid = str(callback.from_user.id)
    s = get_student(uid)
    if not s:
        await callback.message.answer("Ты не зарегистрирован(а). Напиши /start")
        await callback.answer()
        return

    enr = get_enrollments(s)
    ready = [k for k in sorted_enroll_keys(s) if enr[k].get("price") is not None]
    if not ready:
        await callback.message.answer("Виктория ещё не настроила твой абонемент — скоро будет готово!", reply_markup=student_menu())
        await callback.answer()
        return

    if len(ready) == 1:
        await start_payment_report(callback.message, state, uid, ready[0])
    else:
        b = InlineKeyboardBuilder()
        for k in ready:
            b.button(text=enroll_label(k), callback_data=f"paidfor_{k}")
        b.adjust(1)
        await callback.message.answer("За какие занятия оплата?", reply_markup=b.as_markup())
    await callback.answer()


@dp.callback_query(F.data.startswith("paidfor_"))
async def paid_for(callback: types.CallbackQuery, state: FSMContext):
    uid = str(callback.from_user.id)
    key = callback.data.replace("paidfor_", "")
    await start_payment_report(callback.message, state, uid, key)
    await callback.answer()


@dp.message(StudentReportPayment.waiting_lessons)
async def student_report_lessons(message: types.Message, state: FSMContext):
    uid = str(message.from_user.id)
    try:
        lessons = int(message.text.strip())
        if lessons <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Введи целое положительное число, например: 2")
        return

    data = await state.get_data()
    key = data.get("report_key")
    await state.clear()

    db = load_db()
    s = db.get(uid)
    e = get_enrollments(s or {}).get(key)
    if not s or e is None:
        await message.answer("Не нашёл такой абонемент. Напиши /start")
        return

    amount = lessons * (e.get("price") or 0)
    e["pending_lessons"] = lessons
    e["awaiting_confirmation"] = True
    save_db(db)

    b = InlineKeyboardBuilder()
    b.button(text="✅ Подтвердить оплату", callback_data=f"confirmpaid_{uid}|{key}")
    b.adjust(1)
    await bot.send_message(
        ADMIN_ID,
        f"🥰 {s['name']} говорит, что оплатил(а){enroll_for(s, key)} {lessons} {lessons_word(lessons)} ({amount}₽) "
        f"— без предварительного напоминания.\n\nПодтверди, чтобы зачесть оплату:",
        reply_markup=b.as_markup()
    )
    await message.answer("Спасибо! Отправляю Виктории на подтверждение.", reply_markup=student_menu())


@dp.callback_query(F.data.startswith("confirmpaid_"))
async def confirm_paid(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    uid, key = callback.data.replace("confirmpaid_", "").split("|", 1)
    db = load_db()
    s = db.get(uid)
    e = get_enrollments(s or {}).get(key)
    if not s or e is None:
        await callback.answer("Ученик или абонемент не найден", show_alert=True)
        return

    lessons = e.get("pending_lessons") or 0
    e["balance"] = (e.get("balance") or 0) + lessons
    reset_enrollment_reminder(e)
    save_db(db)

    await callback.message.edit_text(
        f"Оплата подтверждена: {s['name']}{enroll_tag(s, key)}\n"
        f"+{lessons} {lessons_word(lessons)} (баланс: {e['balance']} {lessons_word(e['balance'])})"
    )
    try:
        await bot.send_message(int(uid), "Спасибо за оплату!", reply_markup=student_menu())
    except Exception as ex:
        logging.error(f"Не удалось уведомить {uid}: {ex}")
    await callback.answer()


# ============================================================
# Напоминание об оплате (сумма считается автоматически)
# ============================================================
@dp.callback_query(F.data == "remind_menu")
async def remind_menu(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    db = load_db()
    b = InlineKeyboardBuilder()
    for uid, s in db.items():
        if not s.get("active") or s.get("paused"):
            continue
        for key in sorted_enroll_keys(s):
            e = get_enrollments(s)[key]
            if e.get("price") is not None:
                b.button(
                    text=f"{s['name']} · {enroll_short(key)} (баланс: {e.get('balance', 0)})",
                    callback_data=f"remind_pick_{uid}|{key}"
                )
    b.adjust(1)
    await callback.message.answer("Кому напомнить об оплате?", reply_markup=b.as_markup())
    await callback.answer()


@dp.callback_query(F.data.startswith("remind_pick_"))
async def remind_pick(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    uid, key = callback.data.replace("remind_pick_", "").split("|", 1)
    s = get_student(uid)
    e = get_enrollments(s or {}).get(key)
    if not s or e is None:
        await callback.answer("Не найден", show_alert=True)
        return
    await state.update_data(remind_uid=uid, remind_key=key)
    await callback.message.answer(
        f"{s['name']} — {enroll_label(key)}\nЦена урока: {e['price']}₽\n\nСколько уроков нужно оплатить?"
    )
    await state.set_state(SendReminder.waiting_lessons)
    await callback.answer()


@dp.message(SendReminder.waiting_lessons)
async def remind_send(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    try:
        lessons = int(message.text.strip())
        if lessons <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Введи положительное целое число, например: 2")
        return

    data = await state.get_data()
    uid, key = data.get("remind_uid"), data.get("remind_key")
    db = load_db()
    s = db.get(uid)
    e = get_enrollments(s or {}).get(key)
    if not s or e is None:
        await message.answer("Ученик не найден.", reply_markup=admin_menu())
        await state.clear()
        return

    amount = lessons * e["price"]
    e["pending_lessons"] = lessons
    e["reminded_at"] = str(date.today())
    e["followup_sent"] = False
    e["awaiting_confirmation"] = False
    save_db(db)
    await state.clear()

    try:
        await bot.send_message(
            int(uid), build_reminder_text(s, key, lessons),
            parse_mode="HTML", reply_markup=student_menu()
        )
        await message.answer(
            f"Напоминание отправлено: {s['name']}{enroll_tag(s, key)}, {lessons} {lessons_word(lessons)}, {amount}₽.",
            reply_markup=admin_menu()
        )
    except Exception as ex:
        await message.answer(f"Не удалось отправить: {ex}", reply_markup=admin_menu())


async def send_followup_reminders():
    """Раз в день, в 11:00 по МСК, шлёт РОВНО ОДНО повторное напоминание —
    если прошёл 1 день с момента ручного напоминания, а оплата так и не
    отмечена. Дальше бот сам больше не напоминает."""
    while True:
        now = now_msk()
        next_run = now.replace(hour=11, minute=0, second=0, microsecond=0)
        if now >= next_run:
            next_run += timedelta(days=1)
        await asyncio.sleep((next_run - now).total_seconds())

        db = load_db()
        changed = False
        for uid, s in db.items():
            if not s.get("active") or s.get("paused"):
                continue
            for key, e in get_enrollments(s).items():
                if not e.get("pending_lessons") or e.get("followup_sent") or e.get("awaiting_confirmation"):
                    continue
                reminded_at = e.get("reminded_at")
                if not reminded_at:
                    continue
                try:
                    r_date = datetime.strptime(reminded_at, "%Y-%m-%d").date()
                except ValueError:
                    continue
                if (date.today() - r_date).days != 1:
                    continue
                try:
                    await bot.send_message(
                        int(uid), build_reminder_text(s, key, e["pending_lessons"], followup=True),
                        parse_mode="HTML", reply_markup=student_menu()
                    )
                    e["followup_sent"] = True
                    changed = True
                except Exception as ex:
                    logging.error(f"Followup reminder error {uid}: {ex}")
        if changed:
            save_db(db)


# ============================================================
# Настройка подписки ученика (цена урока + стартовый баланс).
# Работает и для добавления ученика в новые занятия: если подписки ещё нет —
# она создаётся в момент сохранения.
# ============================================================
@dp.callback_query(F.data.startswith("setup_"))
async def setup_student_start(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    uid, key = callback.data.replace("setup_", "").split("|", 1)
    s = get_student(uid)
    if not s or not valid_enroll_key(key):
        await callback.answer("Не найден", show_alert=True)
        return
    e = get_enrollments(s).get(key) or {}
    await state.update_data(setup_uid=uid, setup_key=key)
    current_price = e.get("price")
    await callback.message.answer(
        f"{s['name']} — {enroll_label(key)}\n"
        f"Текущая цена урока: {current_price if current_price is not None else 'не задана'}\n\n"
        f"Введи цену одного урока в рублях:"
    )
    await state.set_state(SetupStudent.waiting_price)
    await callback.answer()


@dp.message(SetupStudent.waiting_price)
async def setup_price(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    try:
        price = int(message.text.strip())
    except ValueError:
        await message.answer("Введи целое число, например: 3000")
        return
    await state.update_data(price=price)
    data = await state.get_data()
    s = get_student(data["setup_uid"]) or {}
    e = get_enrollments(s).get(data["setup_key"]) or {}
    current_balance = e.get("balance")
    await message.answer(
        f"Цена: {price}₽\n\nТекущий баланс: {current_balance if current_balance is not None else 'не задан'}\n\n"
        f"Введи стартовый баланс уроков (может быть отрицательным, например -1, 0, 2):"
    )
    await state.set_state(SetupStudent.waiting_balance)


@dp.message(SetupStudent.waiting_balance)
async def setup_balance(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    try:
        balance = int(message.text.strip())
    except ValueError:
        await message.answer("Введи целое число, например: 0 или -1")
        return

    data = await state.get_data()
    uid, key, price = data["setup_uid"], data["setup_key"], data["price"]
    db = load_db()
    if uid not in db:
        await message.answer("Ученик не найден.", reply_markup=admin_menu())
        await state.clear()
        return

    enr = db[uid].setdefault("enrollments", {})
    e = enr.setdefault(key, new_enrollment())
    e["price"] = price
    e["balance"] = balance
    save_db(db)
    s = db[uid]
    await state.clear()

    await message.answer(
        f"Готово! {s['name']} — {enroll_label(key)}: цена {price}₽/урок, баланс {balance} {lessons_word(balance)}.",
        reply_markup=admin_menu()
    )
    try:
        await bot.send_message(
            int(uid),
            f"Твой абонемент настроен Викторией — {enroll_label(key)}:\n"
            f"Цена урока: {price}₽\nБаланс: {balance} {lessons_word(balance)}",
            reply_markup=student_menu()
        )
    except Exception as ex:
        logging.error(f"Не удалось уведомить {uid}: {ex}")


# ============================================================
# Добавить существующего ученика в новые занятия (индивидуальные / группа)
# ============================================================
@dp.callback_query(F.data == "enroll_menu")
async def enroll_menu(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    db = load_db()
    b = InlineKeyboardBuilder()
    count = 0
    for uid, s in db.items():
        if not s.get("active"):
            continue
        missing = [k for k in all_enroll_keys() if k not in get_enrollments(s)]
        if missing:
            b.button(text=f"{s['name']} (сейчас: {mode_labels(s)})", callback_data=f"enroll_pick_{uid}")
            count += 1
    b.adjust(1)
    if count == 0:
        await callback.message.answer("Все ученики уже подключены ко всем видам занятий.", reply_markup=admin_menu())
    else:
        await callback.message.answer("Кого добавить в занятия?", reply_markup=b.as_markup())
    await callback.answer()


@dp.callback_query(F.data.startswith("enroll_pick_"))
async def enroll_pick(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    uid = callback.data.replace("enroll_pick_", "")
    s = get_student(uid)
    if not s:
        await callback.answer("Не найден", show_alert=True)
        return
    b = InlineKeyboardBuilder()
    for k in all_enroll_keys():
        if k not in get_enrollments(s):
            b.button(text=f"➕ {enroll_label(k)}", callback_data=f"setup_{uid}|{k}")
    b.adjust(1)
    await callback.message.answer(
        f"{s['name']} сейчас: {mode_labels(s)}\n\nКуда добавить? (дальше я спрошу цену урока и стартовый баланс)",
        reply_markup=b.as_markup()
    )
    await callback.answer()


# ============================================================
# Список учеников
# ============================================================
@dp.callback_query(F.data == "admin_list")
async def admin_list(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    db = load_db()
    active = {uid: s for uid, s in db.items() if s.get("active")}
    if not active:
        await callback.message.answer("Учеников пока нет.", reply_markup=admin_menu())
        await callback.answer()
        return

    text = "👥 <b>Ученики:</b>\n"
    b = InlineKeyboardBuilder()
    for uid, s in active.items():
        paused = " ⏸" if s.get("paused") else ""
        text += f"\n• <b>{h(s['name'])}</b>{paused}\n"
        for key in sorted_enroll_keys(s):
            e = get_enrollments(s)[key]
            price, balance = e.get("price"), e.get("balance")
            price_str = f"{price}₽" if price is not None else "не задана"
            bal_str = f"{balance} {lessons_word(balance)}" if balance is not None else "не задан"
            text += f"   — {h(enroll_label(key))}: цена {price_str}, баланс {bal_str}\n"
            b.button(text=f"✏️ {s['name']} · {enroll_short(key)}", callback_data=f"setup_{uid}|{key}")
    b.adjust(1)

    if len(text) > 4000:
        text = text[:3900] + "\n..."
    await callback.message.answer(text, parse_mode="HTML")
    await callback.message.answer("Изменить цену/баланс:", reply_markup=b.as_markup())
    await callback.answer()


# ============================================================
# Провести урок (списание -1)
# ============================================================
@dp.callback_query(F.data == "lesson_menu")
async def lesson_menu(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    db = load_db()
    b = InlineKeyboardBuilder()
    for uid, s in db.items():
        if s.get("active") and not s.get("paused") and INDIVIDUAL in get_enrollments(s):
            b.button(text=f"👤 {s['name']}", callback_data=f"marklesson_ind_{uid}")
    for key, label in GROUPS.items():
        b.button(text=f"👥 Группа {label}", callback_data=f"marklesson_group_{key}")
    b.adjust(1)
    await callback.message.answer("У кого провели занятие?", reply_markup=b.as_markup())
    await callback.answer()


@dp.callback_query(F.data.startswith("marklesson_ind_"))
async def marklesson_individual(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    uid = callback.data.replace("marklesson_ind_", "")
    db = load_db()
    s = db.get(uid)
    e = get_enrollments(s or {}).get(INDIVIDUAL)
    if not s or e is None:
        await callback.answer("Не найден", show_alert=True)
        return
    e["balance"] = (e.get("balance") or 0) - 1
    save_db(db)
    await callback.message.answer(
        f"{s['name']} — индивидуально: занятие отмечено. Баланс: {e['balance']} {lessons_word(e['balance'])}",
        reply_markup=admin_menu()
    )
    try:
        await bot.send_message(
            int(uid),
            f"Занятие отмечено{enroll_tag(s, INDIVIDUAL)}. Баланс: {e['balance']} {lessons_word(e['balance'])}",
            reply_markup=student_menu()
        )
    except Exception as ex:
        logging.error(f"Не удалось уведомить {uid}: {ex}")
    await callback.answer()


@dp.callback_query(F.data.startswith("marklesson_group_"))
async def marklesson_group(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    group_key = callback.data.replace("marklesson_group_", "")
    db = load_db()
    members = [(uid, s) for uid, s in db.items()
               if s.get("active") and not s.get("paused") and group_key in get_enrollments(s)]
    if not members:
        await callback.message.answer("В этой группе нет активных учеников.", reply_markup=admin_menu())
        await callback.answer()
        return

    lines = []
    for uid, s in members:
        e = get_enrollments(s)[group_key]
        e["balance"] = (e.get("balance") or 0) - 1
        lines.append(f"{s['name']}: {e['balance']} {lessons_word(e['balance'])}")
        try:
            await bot.send_message(
                int(uid),
                f"Занятие отмечено{enroll_tag(s, group_key)}. Баланс: {e['balance']} {lessons_word(e['balance'])}",
                reply_markup=student_menu()
            )
        except Exception as ex:
            logging.error(f"Не удалось уведомить {uid}: {ex}")
    save_db(db)
    await callback.message.answer("Занятие отмечено для группы:\n" + "\n".join(lines), reply_markup=admin_menu())
    await callback.answer()


# ============================================================
# Пауза / удаление / реактивация (на уровне ученика целиком)
# ============================================================
@dp.callback_query(F.data == "manage_menu")
async def manage_menu(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    b = InlineKeyboardBuilder()
    b.button(text="Пауза / снять паузу", callback_data="manage_pause")
    b.button(text="Удалить ученика", callback_data="manage_remove")
    b.adjust(1)
    await callback.message.answer("Что сделать?", reply_markup=b.as_markup())
    await callback.answer()


@dp.callback_query(F.data == "manage_pause")
async def manage_pause(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    db = load_db()
    b = InlineKeyboardBuilder()
    for uid, s in db.items():
        if s.get("active"):
            icon = "⏸" if s.get("paused") else "▶️"
            b.button(text=f"{icon} {s['name']}", callback_data=f"pause_{uid}")
    b.adjust(1)
    await callback.message.answer("Выбери ученика:", reply_markup=b.as_markup())
    await callback.answer()


@dp.callback_query(F.data.startswith("pause_"))
async def toggle_pause(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    uid = callback.data.replace("pause_", "")
    db = load_db()
    if uid in db:
        db[uid]["paused"] = not db[uid].get("paused", False)
        status = "на паузе" if db[uid]["paused"] else "▶️ активен"
        save_db(db)
        await callback.message.answer(f"{db[uid]['name']}: {status}", reply_markup=admin_menu())
    await callback.answer()


@dp.callback_query(F.data == "manage_remove")
async def manage_remove(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    db = load_db()
    b = InlineKeyboardBuilder()
    for uid, s in db.items():
        if s.get("active"):
            b.button(text=s["name"], callback_data=f"remove_{uid}")
    b.adjust(1)
    await callback.message.answer("Кого удалить?", reply_markup=b.as_markup())
    await callback.answer()


@dp.callback_query(F.data.startswith("remove_"))
async def do_remove(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    uid = callback.data.replace("remove_", "")
    db = load_db()
    if uid in db:
        name = db[uid]["name"]
        db[uid]["active"] = False
        save_db(db)
        await callback.message.answer(f"{name} удалён(а). Данные сохранены.", reply_markup=admin_menu())
    await callback.answer()


@dp.callback_query(F.data == "reactivate_menu")
async def reactivate_menu(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    db = load_db()
    inactive = [(uid, s) for uid, s in db.items() if not s.get("active")]
    if not inactive:
        await callback.message.answer("Нет деактивированных учеников.", reply_markup=admin_menu())
        await callback.answer()
        return
    b = InlineKeyboardBuilder()
    for uid, s in inactive:
        b.button(text=f"{s['name']} ({mode_labels(s)})", callback_data=f"reactivate_{uid}")
    b.adjust(1)
    await callback.message.answer("Кого вернуть?", reply_markup=b.as_markup())
    await callback.answer()


@dp.callback_query(F.data.startswith("reactivate_"))
async def do_reactivate(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    uid = callback.data.replace("reactivate_", "")
    db = load_db()
    if uid in db:
        db[uid]["active"] = True
        db[uid]["paused"] = False
        for e in get_enrollments(db[uid]).values():
            reset_enrollment_reminder(e)
        save_db(db)
        name = db[uid]["name"]
        await callback.message.answer(f"{name} реактивирован(а)!", reply_markup=admin_menu())
        try:
            await bot.send_message(int(uid), "Добро пожаловать обратно! Ваш аккаунт снова активен.", reply_markup=student_menu())
        except Exception as ex:
            logging.error(f"Не удалось уведомить {uid}: {ex}")
    await callback.answer()


# ============================================================
# Номер карты для оплаты
# ============================================================
@dp.message(Command("card"))
async def cmd_card(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    args = message.text.strip().split(maxsplit=1)
    if len(args) < 2 or not args[1].strip():
        await message.answer(
            f"Текущий номер карты: <code>{h(get_card_number())}</code>\n\nЧтобы изменить: /card 1234567890123456",
            parse_mode="HTML"
        )
        return
    new_card = args[1].strip()
    settings = load_settings()
    settings["card_number"] = new_card
    save_settings(settings)
    await message.answer(f"Номер карты обновлён: <code>{h(new_card)}</code>", parse_mode="HTML", reply_markup=admin_menu())


@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer("Панель управления:", reply_markup=admin_menu())


# ============================================================
# Запуск
# ============================================================
async def on_startup():
    migrate_students()
    asyncio.create_task(send_followup_reminders())
    import sys
    from miniapp import start_miniapp
    await start_miniapp(sys.modules[__name__])
    logging.info("Бот Виктории запущен!")

async def main():
    dp.startup.register(on_startup)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    asyncio.run(main())
