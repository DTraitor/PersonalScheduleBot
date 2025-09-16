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
from telegram.constants import ParseMode
import pytz

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
    sept1 = date(reference_year, 9, 1)
    if check_date < sept1:
        raise ValueError("check_date must not be before September 1 of the given year")
    weeks = (check_date - sept1).days // 7
    return 1 if weeks % 2 == 0 else 2


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private(update):
        await update.message.reply_text("Бот працює лише у приватних повідомленнях.")
        return

    tg_id = update.message.from_user.id
    if user_exists(tg_id):
        await update.message.reply_html(
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
    context.user_data[EXPECTING_MANUAL_GROUP] = True
    text = (
        "Вітаю! Введіть код вашої групи (наприклад: Ба-121-22-4-ПІ)."
        if greeting
        else "Введіть новий код вашої групи (наприклад: Ба-121-22-4-ПІ)."
    )
    await update.message.reply_text(text, reply_markup=ForceReply(selective=True))


async def manual_group_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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


def build_schedule_nav_keyboard(target_date: datetime) -> List[List[InlineKeyboardButton]]:
    cur_date = target_date.strftime("%Y-%m-%d")
    return [[
        InlineKeyboardButton("◀️ Попередній", callback_data=f"SCH_NAV|{cur_date}|PREV"),
        InlineKeyboardButton("Наступний ▶️", callback_data=f"SCH_NAV|{cur_date}|NEXT"),
    ]]


async def render_schedule(update_or_query, context: ContextTypes.DEFAULT_TYPE, target_date: datetime = None, from_callback: bool = False):
    """General function to get schedule, apply timezone, render, and send UTC datetime."""
    tg_id = update_or_query.from_user.id if from_callback else update_or_query.message.from_user.id

    if not user_exists(tg_id):
        if not from_callback:
            await update_or_query.message.reply_html("Ви ще не вибрали групу.")
            await ask_for_group(update_or_query, context)
        return

    # Use current date if not provided
    user_tz = pytz.timezone("Europe/Kiev")  # Replace with user-specific TZ if available
    now = datetime.now(tz=user_tz)
    target_date = target_date.astimezone(user_tz) if target_date else now

    # Fetch lessons (API expects naive datetime)
    lessons = get_schedule(target_date.replace(tzinfo=None) - timedelta(hours=24), tg_id)
    lessons.sort(key=lambda l: l.begin_time)

    text = generate_telegram_message_from_list(lessons, target_date, week_parity(target_date.year, target_date.date()))
    kb = build_schedule_nav_keyboard(target_date)

    if from_callback:
        try:
            await update_or_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(kb))
        except Exception:
            pass
    else:
        await update_or_query.message.reply_html(text, reply_markup=InlineKeyboardMarkup(kb))


async def schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.args:
        try:
            given_date = datetime.strptime(context.args[0], "%d.%m").replace(year=datetime.now().year)
        except ValueError:
            await update.message.reply_html("Невірний формат дати. Використовуйте <code>ДД.MM</code> (наприклад, 20.09).")
            return
    else:
        given_date = None

    await render_schedule(update, context, target_date=given_date)


async def tomorrow_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    target = datetime.now() + timedelta(days=1)
    await render_schedule(update, context, target_date=target)


async def callback_query_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    parts = (query.data or "").split("|")
    if len(parts) != 3 or parts[0] != "SCH_NAV":
        return

    try:
        current_date = datetime.strptime(parts[1], "%Y-%m-%d")
    except ValueError:
        return

    new_date = current_date + timedelta(days=-1 if parts[2] == "PREV" else 1 if parts[2] == "NEXT" else 0)
    await render_schedule(query, context, target_date=new_date, from_callback=True)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_html(
        "/start - почати\n"
        "/change_group - змінити групу\n"
        "/schedule [<code>DD.MM</code>] - розклад на сьогодні або дату\n"
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
