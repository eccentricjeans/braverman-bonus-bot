import asyncio
import calendar
import logging
import os
import sqlite3
import warnings
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Iterator
from functools import wraps

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, ConversationHandler, MessageHandler, filters
from telegram.warnings import PTBUserWarning

load_dotenv()

# This bot deliberately combines button callbacks with text input in each
# conversation. PTB emits this advisory warning for that supported setup.
warnings.filterwarnings(
    "ignore",
    message=r"If 'per_message=False', 'CallbackQueryHandler' will not be tracked for every message.*",
    category=PTBUserWarning,
)

TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_IDS = {int(value.strip()) for value in os.environ.get("ADMIN_IDS", "").split(",") if value.strip().isdigit()}
DB_PATH = Path(os.environ.get("DATABASE_PATH", "data/bonus_bot.sqlite3"))

SEARCH_PHONE, ADD_PURCHASE, WRITE_OFF, SET_PERCENT, SET_MONTHS, ADD_CLIENT_PHONE, ADD_CLIENT_NAME, ADD_CLIENT_LASTNAME = range(8)


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def date_text(value: str) -> str:
    return datetime.fromisoformat(value).strftime("%d.%m.%Y")


def money(value: int | Decimal) -> str:
    return f"{int(value):,}".replace(",", " ")


def add_months(value: datetime, months: int) -> datetime:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def init_db() -> None:
    with db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                telegram_id INTEGER UNIQUE,
                first_name TEXT NOT NULL,
                last_name TEXT NOT NULL DEFAULT '',
                phone TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                percent TEXT NOT NULL,
                valid_months INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS bonus_lots (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                original_amount INTEGER NOT NULL,
                remaining_amount INTEGER NOT NULL,
                purchase_amount INTEGER NOT NULL,
                accrued_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_users_phone ON users(phone);
            CREATE INDEX IF NOT EXISTS idx_lots_active ON bonus_lots(user_id, expires_at, remaining_amount);
        """)
        conn.execute(
            "INSERT OR IGNORE INTO settings (id, percent, valid_months, updated_at) VALUES (1, '5', 12, ?)",
            (utcnow().isoformat(),),
        )


def is_admin(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id in ADMIN_IDS)


def admin_only(func):
    """Blocks callback actions even if a client forwards an admin message."""
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_admin(update):
            if update.callback_query:
                await update.callback_query.answer("Эта функция доступна только администратору.", show_alert=True)
            return ConversationHandler.END
        return await func(update, context)
    return wrapped


def get_settings() -> sqlite3.Row:
    with db() as conn:
        return conn.execute("SELECT percent, valid_months FROM settings WHERE id = 1").fetchone()


def get_user_by_tg(telegram_id: int) -> sqlite3.Row | None:
    with db() as conn:
        return conn.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()


def active_lots(user_id: int) -> list[sqlite3.Row]:
    with db() as conn:
        return conn.execute(
            "SELECT * FROM bonus_lots WHERE user_id = ? AND remaining_amount > 0 AND expires_at > ? ORDER BY expires_at, id",
            (user_id, utcnow().isoformat()),
        ).fetchall()


def balance(user_id: int) -> int:
    return sum(row["remaining_amount"] for row in active_lots(user_id))


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔎 Найти пользователя", callback_data="search")],
        [InlineKeyboardButton("➕ Добавить клиента", callback_data="add_client")],
        [InlineKeyboardButton("⚙️ Настройка бонусной системы", callback_data="settings")],
    ])


def card_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Начислить бонусы", callback_data=f"add:{user_id}")],
        [InlineKeyboardButton("➖ Списать бонусы", callback_data=f"writeoff:{user_id}")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="menu")],
    ])


def user_card(user: sqlite3.Row) -> str:
    return (
        f"<b>Карточка пользователя</b>\n\n"
        f"{user['last_name']} {user['first_name']}\n"
        f"Телефон: {user['phone']}\n"
        f"Сумма трат: {money(user['total_spent'])} ₽\n"
        f"Доступно бонусов: {money(user['bonus_balance'])} ₽"
    )


def card_row(user_id: int) -> sqlite3.Row | None:
    with db() as conn:
        return conn.execute("""
            SELECT u.*, COALESCE(SUM(l.purchase_amount), 0) AS total_spent,
                   COALESCE(SUM(CASE WHEN l.remaining_amount > 0 AND l.expires_at > ? THEN l.remaining_amount ELSE 0 END), 0) AS bonus_balance
            FROM users u LEFT JOIN bonus_lots l ON l.user_id = u.id
            WHERE u.id = ? GROUP BY u.id
        """, (utcnow().isoformat(), user_id)).fetchone()



@admin_only
async def add_client_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Введите номер телефона нового клиента.")
    return ADD_CLIENT_PHONE


@admin_only
async def add_client_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    phone = "+" + "".join(ch for ch in update.message.text if ch.isdigit())
    if len(phone) < 5:
        await update.message.reply_text("Введите корректный номер телефона.")
        return ADD_CLIENT_PHONE
    with db() as conn:
        exists = conn.execute("SELECT id FROM users WHERE phone = ?", (phone,)).fetchone()
    if exists:
        await update.message.reply_text("Такой клиент уже существует.")
        return ConversationHandler.END
    context.user_data["new_client_phone"] = phone
    await update.message.reply_text("Введите имя клиента.")
    return ADD_CLIENT_NAME


@admin_only
async def add_client_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["new_client_name"] = update.message.text.strip()
    await update.message.reply_text("Введите фамилию клиента.")
    return ADD_CLIENT_LASTNAME


@admin_only
async def add_client_lastname(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    with db() as conn:
        conn.execute(
            "INSERT INTO users (telegram_id, first_name, last_name, phone, created_at) VALUES (?, ?, ?, ?, ?)",
            (None, context.user_data.get("new_client_name",""), update.message.text.strip(), context.user_data["new_client_phone"], utcnow().isoformat())
        )
    await update.message.reply_text("Клиент добавлен.", reply_markup=main_menu())
    return ConversationHandler.END


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if is_admin(update):
        await update.message.reply_text("<b>Панель администратора</b>\nВыберите действие.", parse_mode=ParseMode.HTML, reply_markup=main_menu())
        return
    user = get_user_by_tg(update.effective_user.id)
    if user:
        await show_client_balance(update, user)
    else:
        keyboard = ReplyKeyboardMarkup([[KeyboardButton("📱 Отправить номер", request_contact=True)]], resize_keyboard=True, one_time_keyboard=True)
        await update.message.reply_text("Чтобы пользоваться бонусами, подтвердите номер телефона кнопкой ниже.", reply_markup=keyboard)


async def register_contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    contact = update.message.contact
    if not contact or contact.user_id != update.effective_user.id:
        await update.message.reply_text("Отправьте свой контакт через кнопку — так бот сможет подтвердить номер.")
        return
    person = update.effective_user
    phone = "+" + "".join(ch for ch in contact.phone_number if ch.isdigit())
    with db() as conn:
        existing = conn.execute("SELECT id FROM users WHERE telegram_id = ?", (person.id,)).fetchone()
        try:
            if existing:
                conn.execute("UPDATE users SET first_name = ?, last_name = ?, phone = ? WHERE telegram_id = ?", (person.first_name, person.last_name or "", phone, person.id))
            else:
                by_phone = conn.execute("SELECT id FROM users WHERE phone = ?", (phone,)).fetchone()
                if by_phone:
                    conn.execute("UPDATE users SET telegram_id = ?, first_name = ?, last_name = ? WHERE phone = ?",
                                 (person.id, person.first_name, person.last_name or "", phone))
                else:
                    conn.execute("INSERT INTO users (telegram_id, first_name, last_name, phone, created_at) VALUES (?, ?, ?, ?, ?)", (person.id, person.first_name, person.last_name or "", phone, utcnow().isoformat()))
        except sqlite3.IntegrityError:
            await update.message.reply_text("Этот номер уже привязан к другому аккаунту. Обратитесь к администратору.", reply_markup=ReplyKeyboardRemove())
            return
    await update.message.reply_text("Готово — номер подтверждён.", reply_markup=ReplyKeyboardRemove())
    await show_client_balance(update, get_user_by_tg(person.id))


async def show_client_balance(update: Update, user: sqlite3.Row) -> None:
    lots = active_lots(user["id"])
    if lots:
        details = "\n".join(f"• {money(row['remaining_amount'])} ₽ — до {date_text(row['expires_at'])}" for row in lots)
        text = f"<b>Ваш бонусный баланс: {money(sum(row['remaining_amount'] for row in lots))} ₽</b>\n\nДоступные бонусы:\n{details}"
    else:
        text = "<b>Ваш бонусный баланс: 0 ₽</b>\n\nАктивных бонусов нет."
    if update.message:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)
    else:
        await update.callback_query.message.reply_text(text, parse_mode=ParseMode.HTML)


async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = get_user_by_tg(update.effective_user.id)
    if not user:
        await start(update, context)
        return
    await show_client_balance(update, user)


@admin_only
async def open_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("<b>Панель администратора</b>\nВыберите действие.", parse_mode=ParseMode.HTML, reply_markup=main_menu())


@admin_only
async def settings_screen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    config = get_settings()
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Изменить процент", callback_data="setpercent")],
        [InlineKeyboardButton("Изменить срок", callback_data="setmonths")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="menu")],
    ])
    await query.edit_message_text(
        f"<b>Настройка бонусной системы</b>\n\nПроцент начисления: {config['percent']}%\nСрок действия новых бонусов: {config['valid_months']} мес.\n\nИзменения применяются только к будущим начислениям.",
        parse_mode=ParseMode.HTML, reply_markup=keyboard,
    )


@admin_only
async def search_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Введите номер телефона клиента целиком или частично.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data="menu")]]))
    return SEARCH_PHONE


@admin_only
async def search_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    digits = "".join(ch for ch in update.message.text if ch.isdigit())
    if len(digits) < 3:
        await update.message.reply_text("Введите минимум 3 цифры номера.")
        return SEARCH_PHONE
    with db() as conn:
        rows = conn.execute("SELECT id, first_name, last_name, phone FROM users WHERE REPLACE(REPLACE(phone, '+', ''), ' ', '') LIKE ? LIMIT 10", (f"%{digits}%",)).fetchall()
    if not rows:
        await update.message.reply_text("Совпадений нет. Введите другой номер или нажмите «Назад».", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data="menu")]]))
        return SEARCH_PHONE
    buttons = [[InlineKeyboardButton(f"{row['last_name']} {row['first_name']} — {row['phone']}", callback_data=f"card:{row['id']}")] for row in rows]
    buttons.append([InlineKeyboardButton("Назад", callback_data="menu")])
    await update.message.reply_text("Выберите пользователя:", reply_markup=InlineKeyboardMarkup(buttons))
    return ConversationHandler.END


@admin_only
async def open_card(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = int(query.data.split(":")[1])
    user = card_row(user_id)
    if not user:
        await query.edit_message_text("Пользователь не найден.", reply_markup=main_menu())
        return
    await query.edit_message_text(user_card(user), parse_mode=ParseMode.HTML, reply_markup=card_keyboard(user_id))


@admin_only
async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["target_user_id"] = int(query.data.split(":")[1])
    config = get_settings()
    await query.edit_message_text(f"Введите сумму покупки. На неё будет начислено {config['percent']}% бонусами.")
    return ADD_PURCHASE


def parse_positive_amount(raw: str) -> int | None:
    try:
        number = Decimal(raw.strip().replace(" ", "").replace(",", "."))
        if number <= 0 or number != number.to_integral_value():
            return None
        return int(number)
    except Exception:
        return None


@admin_only
async def add_purchase(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    purchase = parse_positive_amount(update.message.text)
    user_id = context.user_data.get("target_user_id")
    if not purchase:
        await update.message.reply_text("Введите целую сумму больше нуля, например: 2500")
        return ADD_PURCHASE
    user = card_row(user_id)
    if not user:
        await update.message.reply_text("Пользователь не найден.")
        return ConversationHandler.END
    config = get_settings()
    earned = int((Decimal(purchase) * Decimal(config["percent"]) / 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    now = utcnow()
    expires = add_months(now, config["valid_months"])
    with db() as conn:
        conn.execute("INSERT INTO bonus_lots (user_id, original_amount, remaining_amount, purchase_amount, accrued_at, expires_at) VALUES (?, ?, ?, ?, ?, ?)", (user_id, earned, earned, purchase, now.isoformat(), expires.isoformat()))
    new_balance = balance(user_id)
    await update.message.reply_text(f"Начислено {money(earned)} ₽.\n\n{user_card(card_row(user_id))}", parse_mode=ParseMode.HTML, reply_markup=card_keyboard(user_id))
    try:
        if user["telegram_id"]:
            await context.bot.send_message(user["telegram_id"], f"Бонусный баланс пополнен на {money(earned)} рублей.\nОбщий баланс бонусов: {money(new_balance)} рублей.")
    except Exception:
        logging.warning("Could not notify user %s", user["id"])
    return ConversationHandler.END


@admin_only
async def writeoff_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    user_id = int(query.data.split(":")[1])
    context.user_data["target_user_id"] = user_id
    await query.edit_message_text(f"Доступно для списания: {money(balance(user_id))} ₽\nВведите сумму списания.")
    return WRITE_OFF


@admin_only
async def writeoff(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    amount = parse_positive_amount(update.message.text)
    user_id = context.user_data.get("target_user_id")
    available = balance(user_id)
    if not amount:
        await update.message.reply_text("Введите целую сумму больше нуля.")
        return WRITE_OFF
    if amount > available:
        await update.message.reply_text(f"Недостаточно бонусов. Доступно: {money(available)} ₽.")
        return WRITE_OFF
    with db() as conn:
        remaining = amount
        lots = conn.execute("SELECT id, remaining_amount FROM bonus_lots WHERE user_id = ? AND remaining_amount > 0 AND expires_at > ? ORDER BY expires_at, id", (user_id, utcnow().isoformat())).fetchall()
        for lot in lots:
            take = min(remaining, lot["remaining_amount"])
            conn.execute("UPDATE bonus_lots SET remaining_amount = remaining_amount - ? WHERE id = ?", (take, lot["id"]))
            remaining -= take
            if remaining == 0:
                break
    user = card_row(user_id)
    remaining_balance = balance(user_id)
    await update.message.reply_text(f"Списано {money(amount)} ₽.\n\n{user_card(user)}", parse_mode=ParseMode.HTML, reply_markup=card_keyboard(user_id))
    try:
        if user["telegram_id"]:
            await context.bot.send_message(user["telegram_id"], f"Списано с бонусного баланса: {money(amount)} рублей.\nОстаток бонусов: {money(remaining_balance)} рублей.")
    except Exception:
        logging.warning("Could not notify user %s", user_id)
    return ConversationHandler.END


@admin_only
async def percent_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Введите новый процент начисления, например: 7.5")
    return SET_PERCENT


@admin_only
async def set_percent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        percent = Decimal(update.message.text.strip().replace(",", "."))
        if percent < 0 or percent > 100:
            raise ValueError
    except Exception:
        await update.message.reply_text("Введите значение от 0 до 100, например: 5")
        return SET_PERCENT
    shown = format(percent.normalize(), "f")
    with db() as conn:
        conn.execute("UPDATE settings SET percent = ?, updated_at = ? WHERE id = 1", (shown, utcnow().isoformat()))
    await update.message.reply_text(f"Процент изменён на {shown}%. Это коснётся только будущих начислений.", reply_markup=main_menu())
    return ConversationHandler.END


@admin_only
async def months_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Введите срок действия новых бонусов в месяцах (от 1 до 120).")
    return SET_MONTHS


@admin_only
async def set_months(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        months = int(update.message.text.strip())
        if not 1 <= months <= 120:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Введите целое число от 1 до 120.")
        return SET_MONTHS
    with db() as conn:
        conn.execute("UPDATE settings SET valid_months = ?, updated_at = ? WHERE id = 1", (months, utcnow().isoformat()))
    await update.message.reply_text(f"Срок изменён на {months} мес. Старые бонусы не продлеваются.", reply_markup=main_menu())
    return ConversationHandler.END


def main() -> None:
    if not TOKEN or TOKEN.startswith("123456:"):
        raise RuntimeError("Укажите BOT_TOKEN в файле .env")
    if not ADMIN_IDS:
        raise RuntimeError("Укажите хотя бы один Telegram ID в ADMIN_IDS")
    # Python 3.14 no longer creates a main-thread event loop implicitly;
    # python-telegram-bot 21 still expects one when run_polling() starts.
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    init_db()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("balance", balance_command))
    app.add_handler(MessageHandler(filters.CONTACT, register_contact))
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(search_start, pattern="^search$")],
        states={SEARCH_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, search_phone)]},
        fallbacks=[CallbackQueryHandler(open_menu, pattern="^menu$")],
        allow_reentry=True,
    ))
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(add_start, pattern="^add:")],
        states={ADD_PURCHASE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_purchase)]},
        fallbacks=[CallbackQueryHandler(open_menu, pattern="^menu$")],
        allow_reentry=True,
    ))
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(writeoff_start, pattern="^writeoff:")],
        states={WRITE_OFF: [MessageHandler(filters.TEXT & ~filters.COMMAND, writeoff)]},
        fallbacks=[CallbackQueryHandler(open_menu, pattern="^menu$")],
        allow_reentry=True,
    ))
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(percent_start, pattern="^setpercent$")],
        states={SET_PERCENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_percent)]},
        fallbacks=[CallbackQueryHandler(open_menu, pattern="^menu$")],
        allow_reentry=True,
    ))
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(months_start, pattern="^setmonths$")],
        states={SET_MONTHS: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_months)]},
        fallbacks=[CallbackQueryHandler(open_menu, pattern="^menu$")],
        allow_reentry=True,
    ))

    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(add_client_start, pattern="^add_client$")],
        states={
            ADD_CLIENT_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_client_phone)],
            ADD_CLIENT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_client_name)],
            ADD_CLIENT_LASTNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_client_lastname)],
        },
        fallbacks=[CallbackQueryHandler(open_menu, pattern="^menu$")],
        allow_reentry=True,
    ))
    app.add_handler(CallbackQueryHandler(open_menu, pattern="^menu$"))
    app.add_handler(CallbackQueryHandler(settings_screen, pattern="^settings$"))
    app.add_handler(CallbackQueryHandler(open_card, pattern="^card:"))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    main()
