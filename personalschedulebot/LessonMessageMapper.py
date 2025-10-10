import re
from datetime import datetime
from personalschedulebot.Lesson import Lesson
from typing import List

months_array = {
    '01': 'січня',
    '02': 'лютого',
    '03': 'березня',
    '04': 'квітня',
    '05': 'травня',
    '06': 'червня',
    '07': 'липня',
    '08': 'серпня',
    '09': 'вересня',
    '10': 'жовтня',
    '11': 'листопада',
    '12': 'грудня'
}

time_array = {
    '08:00': '1️⃣',
    '09:50': '2️⃣',
    '11:40': '3️⃣',
    '13:30': '4️⃣',
    '15:20': '5️⃣',
    '17:10': '6️⃣',
    '19:00': '7️⃣',
}


def generate_telegram_message_from_list(lessons: List[Lesson], date: datetime, week_number: int) -> str:
    result: str = f'<b>Розклад на {date.strftime("%d")} {months_array[date.strftime("%d")]}</b>\n'
    result += f'<b>{date.strftime("%A").capitalize()}</b>, {str(week_number)} тиждень\n\n'
    if not len(lessons):
        return result + f'Протягом дня заняття відсутні! 🥳'
    result += '\n\n'.join([generate_telegram_message(lesson) for lesson in lessons])
    return result

def generate_telegram_message(lesson: Lesson) -> str:
    result: str = ''

    lesson_time = lesson.begin_time.strftime("%H:%M")
    if lesson_time in time_array:
        result += f'{time_array[lesson_time]} '
    else:
        result += f'⏰ '

    result += lesson_time

    result += f' - {lesson.title}'
    result += f' - {lesson.location}\n'

    if lesson.lesson_type is not None:
        result += f'{lesson.lesson_type}'

    if len(lesson.teacher) > 0:
        if result[-1] != '\n':
            result += ' - '
        result += f'{lesson.teacher[0]}'

    return result
