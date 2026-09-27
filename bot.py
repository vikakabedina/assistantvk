import os
import json
import logging
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

# Пока только одна группа, но структура сразу рассчитана на добавление новых
GROUPS = {
    "wednesday": "Wednesday (17:00)",
}

MSK = timezone(timedelta(hours=3))

def now_msk():
    return datetime.now(MSK)

logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())


# ---------- хранение данных ----------
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

class ChangeCard(StatesGroup):
    waiting_card = State()


# ---------- меню ----------
def student_menu():
    b = InlineKeyboardBuilder()
    b.button(text="Я оплатил(а)", callback_data="paid")
    b.button(text="Статус абонемента", callback_data="my_status")
    b.button(text="Написать Виктории", callback_data="contact_teacher")
    b.adjust(1)
    return b.as_markup()

def admin_menu():
    b = InlineKeyboardBuilder()
    b.button(text="👥 Все ученики", callback_data="admin_list")
    b.button(text="📚 Провести урок", callback_data="lesson_menu")
    b.button(text="🔔 Напоминание об оплате", callback_data="remind_menu")
    b.button(text="⏸ Пауза / удаление", callback_data="manage_menu")
    b.button(text="♻️ Реактивировать", callback_data="reactivate_menu")
    b.adjust(1)
    return b.as_markup()

def mode_label(s):
    if s.get("mode") == "individual":
        return "Индивидуальные занятия"
    return GROUPS.get(s.get("group"), s.get("group") or "?")


# ============================================================
# Регистрация
# ============================================================
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    if is_admin(message.from_user.id):
        await message.answer("Панель управления:", reply_markup=admin_menu())
        return

    uid = str(message.from_user.id)
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

    price, balance = s.get("price"), s.get("balance")
    if price is None or balance is None:
        await message.answer(f"Привет, {s['name']}!\n\nВиктория скоро настроит твой абонемент — подожди немного🤓")
        return

    bal_text = f"✅ Осталось: *{balance} {lessons_word(balance)}*" if balance > 0 else f"❗ Баланс: *{balance} {lessons_word(balance)}*"
    await message.answer(
        f"Привет, {s['name']}!\n\n{mode_label(s)}\n💳 Цена урока: {price}₽\n{bal_text}",
        parse_mode="Markdown", reply_markup=student_menu()
    )


@dp.message(Register.waiting_name)
async def reg_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    b = InlineKeyboardBuilder()
    b.button(text="Индивидуально", callback_data="mode_individual")
    b.button(text=f"Группа {GROUPS['wednesday']}", callback_data="mode_group_wednesday")
    b.adjust(1)
    await message.answer("Ты занимаешься индивидуально или в группе?", reply_markup=b.as_markup())


@dp.callback_query(F.data.in_({"mode_individual", "mode_group_wednesday"}))
async def reg_mode(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    name = data.get("name")
    if not name:
        await callback.answer()
        return

    uid = str(callback.from_user.id)
    if callback.data == "mode_individual":
        mode, group = "individual", None
        mode_txt = "индивидуально"
    else:
        mode, group = "group", "wednesday"
        mode_txt = f"в группе {GROUPS['wednesday']}"

    student = {
        "name": name,
        "mode": mode,
        "group": group,
        "price": None,
        "balance": None,
        "active": True,
        "paused": False,
        "registered_at": str(date.today()),
        "reminded_at": None,
        "pending_lessons": None,
        "followup_sent": False,
        "awaiting_confirmation": False,
    }
    db = load_db()
    db[uid] = student
    save_db(db)
    await state.clear()

    await callback.message.answer(
        f"Спасибо, {name}! Ты зарегистрирован(а) ({mode_txt}).\n\nВиктория скоро настроит твой абонемент🤓"
    )

    b = InlineKeyboardBuilder()
    b.button(text="⚙️ Настроить", callback_data=f"setup_{uid}")
    b.adjust(1)
    await bot.send_message(
        ADMIN_ID,
        f"Новый ученик:\n{name}\n{mode_txt}\nID: {uid}",
        reply_markup=b.as_markup()
    )
    await callback.answer()


@dp.callback_query(F.data == "contact_teacher")
async def contact_teacher(callback: types.CallbackQuery):
    s = get_student(callback.from_user.id)
    name = s["name"] if s else callback.from_user.full_name
    await bot.send_message(
        ADMIN_ID,
        f"{name} хочет связаться!\nTelegram: @{callback.from_user.username or '—'}\nID: {callback.from_user.id}"
    )
    await callback.message.answer("Сообщение отправлено Виктории🤓")
    await callback.answer()


@dp.callback_query(F.data == "my_status")
async def my_status(callback: types.CallbackQuery):
    s = get_student(callback.from_user.id)
    if not s:
        await callback.message.answer("Ты не зарегистрирован(а). Напиши /start")
        await callback.answer()
        return
    price, balance = s.get("price"), s.get("balance")
    if price is None or balance is None:
        await callback.message.answer("Виктория ещё не настроила твой абонемент — скоро будет готово!", reply_markup=student_menu())
        await callback.answer()
        return
    bal_text = f"✅ Осталось: *{balance} {lessons_word(balance)}*" if balance > 0 else f"❗ Баланс: *{balance} {lessons_word(balance)}*"
    await callback.message.answer(
        f"{s['name']}\n{mode_label(s)}\n\n💳 Цена урока: {price}₽\n{bal_text}",
        parse_mode="Markdown", reply_markup=student_menu()
    )
    await callback.answer()


# ============================================================
# Оплата (с подтверждением админом)
# ============================================================
@dp.callback_query(F.data == "paid")
async def paid_callback(callback: types.CallbackQuery, state: FSMContext):
    uid = str(callback.from_user.id)
    db = load_db()
    s = db.get(uid)
    if not s:
        await callback.message.answer("Ты не зарегистрирован(а). Напиши /start")
        await callback.answer()
        return

    if s.get("pending_lessons"):
        lessons = s["pending_lessons"]
        price = s.get("price") or 0
        amount = lessons * price
        s["awaiting_confirmation"] = True
        save_db(db)
        b = InlineKeyboardBuilder()
        b.button(text="✅ Подтвердить оплату", callback_data=f"confirmpaid_{uid}")
        b.adjust(1)
        await bot.send_message(
            ADMIN_ID,
            f"🥰 {s['name']} говорит, что оплатил(а) {lessons} {lessons_word(lessons)} ({amount}₽).\n\n"
            f"Подтверди, чтобы зачесть оплату:",
            reply_markup=b.as_markup()
        )
        await callback.message.answer("Спасибо! Отправляю Виктории на подтверждение.", reply_markup=student_menu())
    else:
        # Оплата без предварительного напоминания — спрашиваем у САМОГО ученика,
        # сколько уроков он оплатил (а не у Виктории сумму в рублях).
        await callback.message.answer("Сколько уроков ты оплатил(а)? Напиши число:")
        await state.set_state(StudentReportPayment.waiting_lessons)

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
    await state.clear()

    db = load_db()
    s = db.get(uid)
    if not s:
        await message.answer("Ты не зарегистрирован(а). Напиши /start")
        return

    price = s.get("price") or 0
    amount = lessons * price
    s["pending_lessons"] = lessons
    s["awaiting_confirmation"] = True
    save_db(db)

    b = InlineKeyboardBuilder()
    b.button(text="✅ Подтвердить оплату", callback_data=f"confirmpaid_{uid}")
    b.adjust(1)
    await bot.send_message(
        ADMIN_ID,
        f"🥰 {s['name']} говорит, что оплатил(а) {lessons} {lessons_word(lessons)} ({amount}₽) "
        f"— без предварительного напоминания.\n\nПодтверди, чтобы зачесть оплату:",
        reply_markup=b.as_markup()
    )
    await message.answer("Спасибо! Отправляю Виктории на подтверждение.", reply_markup=student_menu())


@dp.callback_query(F.data.startswith("confirmpaid_"))
async def confirm_paid(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    uid = callback.data.replace("confirmpaid_", "")
    db = load_db()
    s = db.get(uid)
    if not s:
        await callback.answer("Ученик не найден", show_alert=True)
        return

    lessons = s.get("pending_lessons") or 0
    s["balance"] = (s.get("balance") or 0) + lessons
    s["pending_lessons"] = None
    s["reminded_at"] = None
    s["followup_sent"] = False
    s["awaiting_confirmation"] = False
    save_db(db)

    await callback.message.edit_text(
        f"Оплата подтверждена: {s['name']}\n+{lessons} {lessons_word(lessons)} (баланс: {s['balance']} {lessons_word(s['balance'])})"
    )
    try:
        await bot.send_message(int(uid), "Спасибо за оплату!", reply_markup=student_menu())
    except Exception as e:
        logging.error(f"Не удалось уведомить {uid}: {e}")
    await callback.answer()
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
        if s.get("active") and not s.get("paused") and s.get("price") is not None:
            b.button(text=f"{s['name']} (баланс: {s.get('balance', 0)})", callback_data=f"remind_pick_{uid}")
    b.adjust(1)
    await callback.message.answer("Кому напомнить об оплате?", reply_markup=b.as_markup())
    await callback.answer()


@dp.callback_query(F.data.startswith("remind_pick_"))
async def remind_pick(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    uid = callback.data.replace("remind_pick_", "")
    s = get_student(int(uid))
    if not s:
        await callback.answer("Не найден", show_alert=True)
        return
    await state.update_data(remind_uid=uid)
    await callback.message.answer(f"{s['name']}\nЦена урока: {s['price']}₽\n\nСколько уроков нужно оплатить?")
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
    uid = data.get("remind_uid")
    db = load_db()
    s = db.get(uid)
    if not s:
        await message.answer("Ученик не найден.", reply_markup=admin_menu())
        await state.clear()
        return

    price = s["price"]
    amount = lessons * price
    s["pending_lessons"] = lessons
    s["reminded_at"] = str(date.today())
    s["followup_sent"] = False
    save_db(db)
    await state.clear()

    card = get_card_number()
    try:
        await bot.send_message(
            int(uid),
            f"{s['name']}, добрый день! Напоминаем об оплате: {lessons} {lessons_word(lessons)} — {amount}₽.\n\n"
            f"Оплатить можно на карту: `{card}`\n\nПосле оплаты нажми кнопку ниже🤓",
            parse_mode="Markdown",
            reply_markup=student_menu()
        )
        await message.answer(
            f"Напоминание отправлено: {s['name']}, {lessons} {lessons_word(lessons)}, {amount}₽.",
            reply_markup=admin_menu()
        )
    except Exception as e:
        await message.answer(f"Не удалось отправить: {e}", reply_markup=admin_menu())


async def send_followup_reminders():
    """Раз в день, в 11:00 по МСК, шлёт РОВНО ОДНО повторное напоминание —
    если прошёл 1 день с момента ручного напоминания, а ученик так и не
    отметил оплату. Дальше бот сам больше не напоминает."""
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
            if not s.get("pending_lessons") or s.get("followup_sent"):
                continue
            reminded_at = s.get("reminded_at")
            if not reminded_at:
                continue
            try:
                r_date = datetime.strptime(reminded_at, "%Y-%m-%d").date()
            except:
                continue
            if (date.today() - r_date).days != 1:
                continue

            lessons = s["pending_lessons"]
            price = s.get("price") or 0
            amount = lessons * price
            card = get_card_number()
            try:
                await bot.send_message(
                    int(uid),
                    f"{s['name']}, напоминаем: не забудьте оплатить {lessons} {lessons_word(lessons)} — {amount}₽.\n\n"
                    f"Оплатить можно на карту: `{card}`\n\nПосле оплаты нажми кнопку ниже🤓",
                    parse_mode="Markdown",
                    reply_markup=student_menu()
                )
                s["followup_sent"] = True
                changed = True
            except Exception as e:
                logging.error(f"Followup reminder error {uid}: {e}")
        if changed:
            save_db(db)


# ============================================================
# Настройка ученика (цена урока + стартовый баланс)
# ============================================================
@dp.callback_query(F.data.startswith("setup_"))
async def setup_student_start(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    uid = callback.data.replace("setup_", "")
    s = get_student(int(uid))
    if not s:
        await callback.answer("Не найден", show_alert=True)
        return
    await state.update_data(setup_uid=uid)
    current_price = s.get("price")
    await callback.message.answer(
        f"{s['name']}\nТекущая цена урока: {current_price if current_price is not None else 'не задана'}\n\n"
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
    uid = data["setup_uid"]
    s = get_student(int(uid)) or {}
    current_balance = s.get("balance")
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
    uid = data["setup_uid"]
    price = data["price"]
    db = load_db()
    if uid not in db:
        await message.answer("Ученик не найден.", reply_markup=admin_menu())
        await state.clear()
        return

    db[uid]["price"] = price
    db[uid]["balance"] = balance
    save_db(db)
    name = db[uid]["name"]
    await state.clear()

    await message.answer(
        f"Готово! {name}: цена {price}₽/урок, баланс {balance} {lessons_word(balance)}.",
        reply_markup=admin_menu()
    )
    try:
        await bot.send_message(
            int(uid),
            f"Твой абонемент настроен Викторией:\nЦена урока: {price}₽\nБаланс: {balance} {lessons_word(balance)}",
            reply_markup=student_menu()
        )
    except Exception as e:
        logging.error(f"Не удалось уведомить {uid}: {e}")


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

    text = "👥 *Ученики:*\n\n"
    b = InlineKeyboardBuilder()
    for uid, s in active.items():
        paused = " ⏸" if s.get("paused") else ""
        price = s.get("price")
        balance = s.get("balance")
        price_str = f"{price}₽" if price is not None else "не задана"
        bal_str = f"{balance} {lessons_word(balance)}" if balance is not None else "не задан"
        text += f"• {s['name']} ({mode_label(s)}){paused} — цена {price_str}, баланс {bal_str}\n"
        b.button(text=f"✏️ {s['name']}", callback_data=f"setup_{uid}")
    b.adjust(1)

    if len(text) > 4000:
        text = text[:3900] + "\n..."
    await callback.message.answer(text, parse_mode="Markdown")
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
        if s.get("active") and not s.get("paused") and s.get("mode") == "individual":
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
    if not s:
        await callback.answer("Не найден", show_alert=True)
        return
    s["balance"] = (s.get("balance") or 0) - 1
    save_db(db)
    await callback.message.answer(
        f"{s['name']}: занятие отмечено. Баланс: {s['balance']} {lessons_word(s['balance'])}",
        reply_markup=admin_menu()
    )
    try:
        await bot.send_message(
            int(uid),
            f"Занятие отмечено. Баланс: {s['balance']} {lessons_word(s['balance'])}",
            reply_markup=student_menu()
        )
    except Exception as e:
        logging.error(f"Не удалось уведомить {uid}: {e}")
    await callback.answer()


@dp.callback_query(F.data.startswith("marklesson_group_"))
async def marklesson_group(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    group_key = callback.data.replace("marklesson_group_", "")
    db = load_db()
    members = [(uid, s) for uid, s in db.items()
               if s.get("active") and not s.get("paused") and s.get("group") == group_key]
    if not members:
        await callback.message.answer("В этой группе нет активных учеников.", reply_markup=admin_menu())
        await callback.answer()
        return

    lines = []
    for uid, s in members:
        s["balance"] = (s.get("balance") or 0) - 1
        lines.append(f"{s['name']}: {s['balance']} {lessons_word(s['balance'])}")
        try:
            await bot.send_message(
                int(uid),
                f"Занятие отмечено. Баланс: {s['balance']} {lessons_word(s['balance'])}",
                reply_markup=student_menu()
            )
        except Exception as e:
            logging.error(f"Не удалось уведомить {uid}: {e}")
    save_db(db)
    await callback.message.answer("Занятие отмечено для группы:\n" + "\n".join(lines), reply_markup=admin_menu())
    await callback.answer()


# ============================================================
# Пауза / удаление / реактивация
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
        b.button(text=f"{s['name']} ({mode_label(s)})", callback_data=f"reactivate_{uid}")
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
        db[uid]["reminded_at"] = None
        db[uid]["pending_lessons"] = None
        db[uid]["followup_sent"] = False
        db[uid]["awaiting_confirmation"] = False
        save_db(db)
        name = db[uid]["name"]
        await callback.message.answer(f"{name} реактивирован(а)!", reply_markup=admin_menu())
        try:
            await bot.send_message(int(uid), "Добро пожаловать обратно! Ваш аккаунт снова активен.", reply_markup=student_menu())
        except Exception as e:
            logging.error(f"Не удалось уведомить {uid}: {e}")
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
        await message.answer(f"Текущий номер карты: `{get_card_number()}`\n\nЧтобы изменить: /card 1234567890123456", parse_mode="Markdown")
        return
    new_card = args[1].strip()
    settings = load_settings()
    settings["card_number"] = new_card
    save_settings(settings)
    await message.answer(f"Номер карты обновлён: `{new_card}`", parse_mode="Markdown", reply_markup=admin_menu())


@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer("Панель управления:", reply_markup=admin_menu())


# ============================================================
# Запуск
# ============================================================
async def on_startup():
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
