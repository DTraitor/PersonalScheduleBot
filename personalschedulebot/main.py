import logging
import os
from datetime import datetime, timedelta, date
from typing import List

from telegram import (
    ForceReply,
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup, BotCommand,
)
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)
from telegram.constants import (
    ParseMode,
)

from .LessonMessageMapper import generate_telegram_message_from_list
from .ScheduleAPI import (
    get_schedule,
    user_exists,
    get_faculties,
    get_groups,
    create_user,
    change_user_group,
)

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

EXPECTING_MANUAL_GROUP = "expecting_manual_group"  # flag in user_data


def is_private(update: Update) -> bool:
    return update.effective_chat and update.effective_chat.type == "private"


def week_parity(reference_year: int, check_date: date = None) -> int:
    if check_date is None:
        check_date = date.today()

    # September 1 of the reference year
    sept1 = date(reference_year, 9, 1)

    # Ensure we always compare starting from that year's Sept 1
    if check_date < sept1:
        raise ValueError("check_date must not be before September 1 of the given year")

    # Calculate how many weeks have passed since Sept 1
    weeks = (check_date - sept1).days // 7

    # Return 1 for odd, 2 for even (relative to Sept 1 being week 0 = even)
    return 1 if weeks % 2 == 0 else 2


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private(update):
        await update.message.reply_text("Бот працює лише у приватних повідомленнях.")
        return

    tg_id = update.message.from_user.id
    if user_exists(tg_id):
        await update.message.reply_html(
            "Цей бот здатен відсилати розклад певної групи на певну дату.\n\n"
            "Ваша група вже встановлена. Використайте команду /schedule щоб отримати розклад на сьогодні або /change_group щоб змінити групу."
        )
        return

    await ask_for_group(update, context, greeting=True)


async def change_group(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private(update):
        await update.message.reply_text("Бот працює лише у приватних повідомленнях.")
        return
    await ask_for_group(update, context)


async def ask_for_group(update: Update, context: ContextTypes.DEFAULT_TYPE, greeting: bool = False) -> None:
    """Ask user to type their group code manually."""
    context.user_data[EXPECTING_MANUAL_GROUP] = True
    text = (
        "Вітаю! Цей бот здатен відсилати розклад певної групи на певну дату.\n\n"
        "Введіть код вашої групи (наприклад: Б-121-22-4-ПІ)."
        if greeting
        else "Введіть новий код вашої групи (наприклад: Б-121-22-4-ПІ)."
    )
    await update.message.reply_text(text, reply_markup=ForceReply(selective=True))


async def manual_group_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle manual group code input or echo messages."""
    if not is_private(update):
        await update.message.reply_text("Бот працює лише у приватних повідомленнях.")
        return

    if context.user_data.get(EXPECTING_MANUAL_GROUP):
        group_code = update.message.text.strip()
        tg_id = update.message.from_user.id

        if user_exists(tg_id):
            ok = change_user_group(tg_id, group_code)
            if ok:
                await update.message.reply_html(f"Група змінена на <b>{group_code}</b>.")
            else:
                await update.message.reply_text("Не вдалося змінити групу. Спробуйте пізніше.")
        else:
            ok = create_user(tg_id, group_code)
            if ok:
                await update.message.reply_html(
                    f"Група встановлена: <b>{group_code}</b>.\nВикористайте /schedule щоб отримати розклад."
                )
            else:
                await update.message.reply_text("Не вдалося створити користувача. Спробуйте пізніше.")

        context.user_data.pop(EXPECTING_MANUAL_GROUP, None)
        return


def build_schedule_nav_keyboard(date: datetime) -> List[List[InlineKeyboardButton]]:
    cur_date = date.strftime("%Y-%m-%d")
    return [[
        InlineKeyboardButton("◀️ Попередній", callback_data=f"SCH_NAV|{cur_date}|PREV"),
        InlineKeyboardButton("Наступний ▶️", callback_data=f"SCH_NAV|{cur_date}|NEXT"),
    ]]


async def schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private(update):
        await update.message.reply_text("Бот працює лише у приватних повідомленнях.")
        return

    tg_id = update.message.from_user.id
    if not user_exists(tg_id):
        await update.message.reply_html("Ви ще не вибрали групу.")
        await ask_for_group(update, context)
        return

    if context.args:
        try:
            given_date = datetime.strptime(context.args[0], "%d.%m").replace(year=datetime.now().year)
        except ValueError:
            await update.message.reply_html("Невірний формат дати. Використовуйте <code>ДД.MM</code> (наприклад, 20.09).")
            return
    else:
        given_date = datetime.now()

    lessons = get_schedule(given_date - timedelta(hours=24), tg_id)
    lessons.sort(key = lambda l: l.begin_time)
    text = generate_telegram_message_from_list(lessons, given_date, week_parity(given_date.year, given_date.date()))
    kb = build_schedule_nav_keyboard(given_date)
    await update.message.reply_html(text, reply_markup=InlineKeyboardMarkup(kb))


async def tomorrow_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private(update):
        await update.message.reply_text("Бот працює лише у приватних повідомленнях.")
        return

    tg_id = update.message.from_user.id
    if not user_exists(tg_id):
        await update.message.reply_html("Ви ще не вибрали групу.")
        await ask_for_group(update, context)
        return

    target = datetime.now() + timedelta(days=1)
    lessons = get_schedule(target - timedelta(hours=24), tg_id)
    lessons.sort(key = lambda l: l.begin_time)
    text = generate_telegram_message_from_list(lessons, target, week_parity(target.year, target.date()))
    kb = build_schedule_nav_keyboard(target)
    await update.message.reply_html(text, reply_markup=InlineKeyboardMarkup(kb))


async def callback_query_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle only schedule navigation now."""
    query = update.callback_query
    await query.answer()
    parts = (query.data or "").split("|")
    if len(parts) != 3 or parts[0] != "SCH_NAV":
        return

    day_str, action = parts[1], parts[2]
    try:
        current_date = datetime.strptime(day_str, "%Y-%m-%d")
    except ValueError:
        return

    if action == "PREV":
        new_date = current_date - timedelta(days=1)
    elif action == "NEXT":
        new_date = current_date + timedelta(days=1)
    else:
        new_date = current_date

    tg_id = query.from_user.id
    lessons = get_schedule(new_date - timedelta(hours=24), tg_id)
    lessons.sort(key = lambda l: l.begin_time)
    text = generate_telegram_message_from_list(lessons, new_date, week_parity(new_date.year, new_date.date()))
    kb = build_schedule_nav_keyboard(new_date)

    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))
    except Exception:
        pass


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_html(
        "/start - почати\n"
        "/change_group - змінити групу\n"
        "/schedule [<code>DD.MM</code>] - розклад на сьогодні або вказану дату\n"
        "/tomorrow - розклад на завтра\n"
    )


def main() -> None:
    token = os.environ["BOT_TOKEN"]
    application = Application.builder().token(token).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("change_group", change_group))
    application.add_handler(CommandHandler(["schedule", "te"], schedule_command))
    application.add_handler(CommandHandler(["tomorrow", "te_t"], tomorrow_command))
    application.add_handler(CommandHandler("help", help_command))

    application.add_handler(CallbackQueryHandler(callback_query_router))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, manual_group_message_handler))

    async def post_init(app: Application):
        await app.bot.set_my_commands([
            BotCommand("start", "Почати роботу з ботом"),
            BotCommand("change_group", "Змінити групу"),
            BotCommand("schedule", "Розклад на сьогодні або дату"),
            BotCommand("tomorrow", "Розклад на завтра"),
            BotCommand("help", "Список команд"),
        ])

    application.post_init = post_init

    application.run_polling(allowed_updates=Update.ALL_TYPES)
