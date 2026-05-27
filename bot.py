import asyncio
import logging
import os
import re
from datetime import datetime

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardRemove, URLInputFile
)
from dotenv import load_dotenv

from numerology import NumerologyCalculator

# === ЗАГРУЗКА ПЕРЕМЕННЫХ ===
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")  # Для будущего использования

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не найден в .env")

# === ИНИЦИАЛИЗАЦИЯ БОТА ===
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# === КНОПКИ МЕНЮ ===
menu_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="ℹ️ Помощь"), KeyboardButton(text="🗑 Очистить диалог")],
        [KeyboardButton(text="❌ Отмена"), KeyboardButton(text="🔮 Число судьбы")],
        [KeyboardButton(text="⭐ Гороскоп"), KeyboardButton(text="♊ Совместимость")],
        [KeyboardButton(text="🎴 Карта дня Таро"), KeyboardButton(text="📞 Запись к психологу")]
    ],
    resize_keyboard=True
)

# === СОСТОЯНИЯ FSM ===
class Dialogue(StatesGroup):
    chatting = State()
    waiting_for_contact = State()
    waiting_for_birthdate = State()
    waiting_for_birthdate_comp = State()
    waiting_for_birthdate_comp2 = State()
    waiting_for_forecast_period = State()
    waiting_for_zodiac = State()

# === ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ===
def is_valid_date(date_str: str) -> bool:
    """Проверка формата даты ДД.ММ.ГГГГ"""
    return bool(re.match(r'^\d{2}\.\d{2}\.\d{4}$', date_str))

# === ОБРАБОТЧИКИ КОМАНД И КНОПОК ===
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "✨ **Приветствую в эзотерическом центре!** ✨\n\n"
        "Я помогу вам узнать число судьбы, совместимость, сделать нумерологический прогноз, "
        "получить карту Таро с изображением и многое другое.\n\n"
        "Используйте кнопки меню или команды:\n"
        "/help - помощь\n"
        "/cancel - отмена",
        reply_markup=menu_keyboard,
        parse_mode="Markdown"
    )

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    help_text = (
        "📖 **Доступные функции:**\n\n"
        "🔮 **Число судьбы** - расчет по дате рождения\n"
        "⭐ **Гороскоп** - астрологический прогноз на сегодня\n"
        "♊ **Совместимость** - анализ пары по датам\n"
        "🎴 **Карта дня Таро** - карта с изображением и значением\n"
        "📞 **Запись к психологу** - оставьте контакт\n\n"
        "🗑 **Очистить диалог** / ❌ **Отмена**"
    )
    await message.answer(help_text, parse_mode="Markdown")

@dp.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Действие отменено.", reply_markup=menu_keyboard)

# === 1. ЧИСЛО СУДЬБЫ (НУМЕРОЛОГИЯ) ===
@dp.message(F.text == "🔮 Число судьбы")
async def fate_number_start(message: types.Message, state: FSMContext):
    await message.answer(
        "🔮 **Расчет числа судьбы**\n\n"
        "Введите вашу дату рождения в формате:\n`ДД.ММ.ГГГГ`\n"
        "Пример: `15.05.1990`",
        parse_mode="Markdown"
    )
    await state.set_state(Dialogue.waiting_for_birthdate)

@dp.message(StateFilter(Dialogue.waiting_for_birthdate))
async def process_fate_number(message: types.Message, state: FSMContext):
    if not is_valid_date(message.text):
        await message.answer("❌ Неверный формат. Введите как `ДД.ММ.ГГГГ`")
        return
    
    number, description = NumerologyCalculator.calculate_fate_number(message.text)
    # Персональный прогноз на день
    daily_forecast = NumerologyCalculator.get_personal_forecast(message.text, "day")
    
    await message.answer(
        f"🔮 **Ваше число судьбы: {number}**\n\n{description}\n\n"
        f"📅 **Прогноз на сегодня:** {daily_forecast}\n\n"
        f"✨ Это число раскрывает ваши врожденные таланты и жизненный путь.",
        parse_mode="Markdown"
    )
    await state.clear()

# === 2. ГОРОСКОП (УПРОЩЕННЫЙ) ===
@dp.message(F.text == "⭐ Гороскоп")
async def horoscope_start(message: types.Message, state: FSMContext):
    await message.answer(
        "⭐ **Астрологический гороскоп**\n\n"
        "Введите ваш знак зодиака или дату рождения:\n"
        "Овен, Телец, Близнецы, Рак, Лев, Дева, Весы, Скорпион, Стрелец, Козерог, Водолей, Рыбы\n\n"
        "Или отправьте дату в формате `ДД.ММ.ГГГГ`",
        parse_mode="Markdown"
    )
    await state.set_state(Dialogue.waiting_for_zodiac)

@dp.message(StateFilter(Dialogue.waiting_for_zodiac))
async def process_horoscope(message: types.Message, state: FSMContext):
    text = message.text.strip()
    zodiac_sign = None
    
    if is_valid_date(text):
        day, month, _ = map(int, text.split('.'))
        # Простейшее определение знака (упрощенно)
        if (month == 3 and day >= 21) or (month == 4 and day <= 19): zodiac_sign = "Овен"
        elif (month == 4 and day >= 20) or (month == 5 and day <= 20): zodiac_sign = "Телец"
        elif (month == 5 and day >= 21) or (month == 6 and day <= 20): zodiac_sign = "Близнецы"
        elif (month == 6 and day >= 21) or (month == 7 and day <= 22): zodiac_sign = "Рак"
        elif (month == 7 and day >= 23) or (month == 8 and day <= 22): zodiac_sign = "Лев"
        elif (month == 8 and day >= 23) or (month == 9 and day <= 22): zodiac_sign = "Дева"
        elif (month == 9 and day >= 23) or (month == 10 and day <= 22): zodiac_sign = "Весы"
        elif (month == 10 and day >= 23) or (month == 11 and day <= 21): zodiac_sign = "Скорпион"
        elif (month == 11 and day >= 22) or (month == 12 and day <= 21): zodiac_sign = "Стрелец"
        elif (month == 12 and day >= 22) or (month == 1 and day <= 19): zodiac_sign = "Козерог"
        elif (month == 1 and day >= 20) or (month == 2 and day <= 18): zodiac_sign = "Водолей"
        else: zodiac_sign = "Рыбы"
        await message.answer(f"♈ Ваш знак: **{zodiac_sign}**")
    else:
        # Простейшая проверка ввода знака
        known = ["овен","телец","близнецы","рак","лев","дева","весы","скорпион","стрелец","козерог","водолей","рыбы"]
        if text.lower() in known:
            zodiac_sign = text.capitalize()
        else:
            await message.answer("❌ Неизвестный знак или неверная дата. Попробуйте еще раз.")
            return
    
    # Простой гороскоп-заготовка (можно позже заменить на LLM)
    forecasts = {
        "Овен": "🔥 Энергия бьет ключом! Начните новые дела, ваша инициатива принесет плоды.",
        "Телец": "💰 Хороший день для финансовых решений. Не торопитесь с тратами, планируйте бюджет.",
        "Близнецы": "💬 День общения и новых знакомств. Полезная информация придет через друзей.",
        "Рак": "🏠 День интуиции и семьи. Займитесь домом, уделите время близким.",
        "Лев": "🎭 Творческий день. Покажите себя, ваши таланты будут замечены.",
        "Дева": "📋 День порядка и планирования. Систематизируйте дела, завершите начатое.",
        "Весы": "⚖️ День гармонии. Избегайте конфликтов, ищите компромиссы.",
        "Скорпион": "🦂 День трансформации. Глубокие размышления помогут найти решение.",
        "Стрелец": "✈️ День приключений и оптимизма. Расширяйте горизонты!",
        "Козерог": "🏔️ День достижений. Работайте над целями, будьте упорны.",
        "Водолей": "💡 День идей и нестандартных решений. Делитесь мыслями!",
        "Рыбы": "🎨 День творчества и интуиции. Займитесь искусством, слушайте сердце."
    }
    forecast = forecasts.get(zodiac_sign, "🌟 Гармоничный день. Доверьтесь своей интуиции.")
    await message.answer(f"✨ **Гороскоп для {zodiac_sign}** ✨\n\n📅 **Сегодня:** {forecast}", parse_mode="Markdown")
    await state.clear()

# === 3. СОВМЕСТИМОСТЬ ===
compatibility_data = {}
@dp.message(F.text == "♊ Совместимость")
async def compatibility_start(message: types.Message, state: FSMContext):
    await message.answer(
        "♊ **Расчет совместимости**\n\nВведите **первую** дату рождения (вашу):\n`ДД.ММ.ГГГГ`",
        parse_mode="Markdown"
    )
    await state.set_state(Dialogue.waiting_for_birthdate_comp)

@dp.message(StateFilter(Dialogue.waiting_for_birthdate_comp))
async def process_compatibility_first(message: types.Message, state: FSMContext):
    if not is_valid_date(message.text):
        await message.answer("❌ Неверный формат. Введите как `ДД.ММ.ГГГГ`")
        return
    compatibility_data['date1'] = message.text
    await message.answer(
        "♊ **Расчет совместимости**\n\nВведите **вторую** дату рождения (партнера):\n`ДД.ММ.ГГГГ`",
        parse_mode="Markdown"
    )
    await state.set_state(Dialogue.waiting_for_birthdate_comp2)

@dp.message(StateFilter(Dialogue.waiting_for_birthdate_comp2))
async def process_compatibility_second(message: types.Message, state: FSMContext):
    if not is_valid_date(message.text):
        await message.answer("❌ Неверный формат. Введите как `ДД.ММ.ГГГГ`")
        return
    date1 = compatibility_data.get('date1')
    if not date1:
        await message.answer("❌ Ошибка. Начните заново командой /start")
        await state.clear()
        return
    
    result = NumerologyCalculator.get_compatibility(date1, message.text)
    if result['compatibility_percent'] == 0:
        await message.answer("❌ Ошибка расчета. Проверьте даты.")
    else:
        await message.answer(
            f"♊ **Совместимость**\n\n"
            f"📅 Дата 1: {date1} ({result['sign1']}, число {result['number1']})\n"
            f"📅 Дата 2: {message.text} ({result['sign2']}, число {result['number2']})\n\n"
            f"💕 **Совместимость: {result['compatibility_percent']}%**\n{result['text']}",
            parse_mode="Markdown"
        )
    compatibility_data.clear()
    await state.clear()

# === 4. КАРТА ДНЯ ТАРО С ИЗОБРАЖЕНИЕМ ===
@dp.message(F.text == "🎴 Карта дня Таро")
async def taro_card_handler(message: types.Message):
    await message.answer("🎴 Вытягиваю карту дня... Подождите немного.")
    try:
        name, image_url, meaning = await NumerologyCalculator.get_taro_card_with_image()
        if image_url:
            # Отправляем картинку через URL (Telegram сам скачает и отправит)
            await message.answer_photo(
                photo=URLInputFile(image_url),
                caption=f"🎴 **Карта дня: {name}**\n\n{meaning}\n\n✨ Энергия этой карты будет сопровождать вас сегодня.",
                parse_mode="Markdown"
            )
        else:
            # Если картинки нет, отправляем просто текст
            await message.answer(
                f"🎴 **Карта дня: {name}**\n\n{meaning}\n\n✨ Энергия этой карты будет сопровождать вас сегодня.",
                parse_mode="Markdown"
            )
    except Exception as e:
        print(f"Ошибка при отправке карты Таро: {e}")
        # Резервный вариант из локального словаря
        name, _, meaning = NumerologyCalculator.get_taro_card_local()
        await message.answer(
            f"🎴 **Карта дня: {name}**\n\n{meaning}\n\n✨ Прислушайтесь к её посланию.",
            parse_mode="Markdown"
        )

# === ЗАПИСЬ К ПСИХОЛОГУ ===
@dp.message(F.text == "📞 Запись к психологу")
async def book_psychologist_start(message: types.Message, state: FSMContext):
    await message.answer(
        "📝 **Запись на консультацию**\n\n"
        "Оставьте ваш контакт (@username или номер телефона), и наш специалист свяжется с вами в ближайшее время.",
        reply_markup=ReplyKeyboardRemove()
    )
    await state.set_state(Dialogue.waiting_for_contact)

@dp.message(StateFilter(Dialogue.waiting_for_contact))
async def process_booking(message: types.Message, state: FSMContext):
    contact = message.text
    await message.answer(
        f"✅ Спасибо! Психолог свяжется с вами в течение 24 часов.\n\nБерегите себя ❤️",
        reply_markup=menu_keyboard
    )
    await state.clear()

# === ОСТАЛЬНЫЕ КНОПКИ МЕНЮ ===
@dp.message(F.text == "ℹ️ Помощь")
async def menu_help(message: types.Message):
    await cmd_help(message)

@dp.message(F.text == "🗑 Очистить диалог")
async def menu_reset(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("🗑 История и состояния очищены.", reply_markup=menu_keyboard)

@dp.message(F.text == "❌ Отмена")
async def menu_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Действие отменено.", reply_markup=menu_keyboard)

# === ОБРАБОТЧИК ТЕКСТОВЫХ СООБЩЕНИЙ (ЕСЛИ НЕТ КОМАНДЫ) ===
@dp.message()
async def echo(message: types.Message):
    await message.answer(
        "Я вас не понял. Пожалуйста, используйте кнопки меню или команду /help.",
        reply_markup=menu_keyboard
    )

# === ЗАПУСК БОТА ===
async def main():
    print("🚀 Бот с нумерологией, гороскопом и картами Таро запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
