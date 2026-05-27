import asyncio
import logging
import os
import re
import random
import json
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardRemove
)
from dotenv import load_dotenv
from openai import AsyncOpenAI
import gspread
from google.oauth2.service_account import Credentials

# ========== ЗАГРУЗКА ПЕРЕМЕННЫХ ==========
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
PSYCHOLOGIST_ID = int(os.getenv("PSYCHOLOGIST_ID", 0))
SHEET_ID = os.getenv("SHEET_ID")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не найден")

# ========== ИНИЦИАЛИЗАЦИЯ GROQ ==========
groq_client = AsyncOpenAI(
    api_key=GROQ_API_KEY,
    base_url="https://api.groq.com/openai/v1"
) if GROQ_API_KEY else None

PSYCHOLOGIST_NAME = "Дарья"

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ========== FSM СОСТОЯНИЯ ==========
class Dialogue(StatesGroup):
    chatting = State()
    waiting_for_contact = State()
    waiting_for_birthdate = State()
    waiting_for_birthdate_comp = State()
    waiting_for_birthdate_comp2 = State()
    waiting_for_zodiac = State()

# ========== ГЛАВНОЕ МЕНЮ ==========
menu_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="ℹ️ Помощь"), KeyboardButton(text="🗑 Очистить диалог")],
        [KeyboardButton(text="🔮 Число судьбы"), KeyboardButton(text="⭐ Гороскоп")],
        [KeyboardButton(text="♊ Совместимость"), KeyboardButton(text="🎴 Карта дня Таро")],
        [KeyboardButton(text="📞 Запись к психологу")]
    ],
    resize_keyboard=True
)

# ========== ПРАВИЛЬНОЕ ОПРЕДЕЛЕНИЕ ЗНАКОВ ЗОДИАКА ==========
def get_zodiac_sign(day: int, month: int) -> str:
    """Точное определение знака зодиака"""
    if (month == 1 and day >= 20) or (month == 2 and day <= 18):
        return "Водолей"
    elif (month == 2 and day >= 19) or (month == 3 and day <= 20):
        return "Рыбы"
    elif (month == 3 and day >= 21) or (month == 4 and day <= 19):
        return "Овен"
    elif (month == 4 and day >= 20) or (month == 5 and day <= 20):
        return "Телец"
    elif (month == 5 and day >= 21) or (month == 6 and day <= 20):
        return "Близнецы"
    elif (month == 6 and day >= 21) or (month == 7 and day <= 22):
        return "Рак"
    elif (month == 7 and day >= 23) or (month == 8 and day <= 22):
        return "Лев"
    elif (month == 8 and day >= 23) or (month == 9 and day <= 22):
        return "Дева"
    elif (month == 9 and day >= 23) or (month == 10 and day <= 22):
        return "Весы"
    elif (month == 10 and day >= 23) or (month == 11 and day <= 21):
        return "Скорпион"
    elif (month == 11 and day >= 22) or (month == 12 and day <= 21):
        return "Стрелец"
    else:
        return "Козерог"

# ========== РАСЧЕТ ЧИСЛА СУДЬБЫ ==========
def calculate_fate_number(birth_date: str) -> tuple:
    try:
        day, month, year = map(int, birth_date.split('.'))
        total = day + month + year
        while total > 9:
            total = sum(int(d) for d in str(total))
        descriptions = {
            1: "Лидер, новатор. Ваша миссия — начинать новое и вдохновлять.",
            2: "Дипломат, миротворец. Ваша задача — создавать гармонию.",
            3: "Творец, коммуникатор. Ваш путь — самовыражение и радость.",
            4: "Строитель, труженик. Ваша миссия — создавать прочные основы.",
            5: "Искатель свободы. Ваш путь — перемены и приключения.",
            6: "Опекун, семьянин. Ваша задача — забота о близких.",
            7: "Исследователь, мудрец. Ваш путь — познание и анализ.",
            8: "Маг, бизнесмен. Ваша миссия — успех и материальная реализация.",
            9: "Гуманист, учитель. Ваш путь — помощь людям."
        }
        return (total, descriptions.get(total, "Уникальная личность"))
    except:
        return (0, "Ошибка формата даты")

# ========== РАСЧЕТ СОВМЕСТИМОСТИ ==========
def get_compatibility(date1: str, date2: str) -> dict:
    try:
        day1, month1, _ = map(int, date1.split('.'))
        day2, month2, _ = map(int, date2.split('.'))
        
        sign1 = get_zodiac_sign(day1, month1)
        sign2 = get_zodiac_sign(day2, month2)
        
        elements = {"Овен": "Огонь", "Лев": "Огонь", "Стрелец": "Огонь",
                    "Телец": "Земля", "Дева": "Земля", "Козерог": "Земля",
                    "Близнецы": "Воздух", "Весы": "Воздух", "Водолей": "Воздух",
                    "Рак": "Вода", "Скорпион": "Вода", "Рыбы": "Вода"}
        
        elem1 = elements.get(sign1, "")
        elem2 = elements.get(sign2, "")
        
        if elem1 == elem2:
            compatibility = random.randint(85, 98)
            text = "🌟 Прекрасная совместимость! Вы на одной волне."
        elif (elem1, elem2) in [("Огонь", "Воздух"), ("Воздух", "Огонь"),
                                 ("Земля", "Вода"), ("Вода", "Земля")]:
            compatibility = random.randint(70, 85)
            text = "💫 Хорошая совместимость! Вы отлично дополняете друг друга."
        else:
            compatibility = random.randint(45, 70)
            text = "🌱 Средняя совместимость. Есть над чем работать!"
        
        return {"percent": compatibility, "text": text, "sign1": sign1, "sign2": sign2}
    except:
        return {"percent": 0, "text": "Ошибка формата даты"}

# ========== ХРАНИЛИЩЕ ==========
user_history = {}
user_problems = {}

SYSTEM_PROMPT = f"""Ты — эмпатичный психолог-помощник по имени {PSYCHOLOGIST_NAME}.

Правила:
1. Внимательно слушай и задавай вопросы.
2. Проявляй эмпатию.
3. Не ставь диагнозы.
4. При кризисе — дай телефон доверия: 8-800-2000-122.
5. После 4-6 обменов мягко предложи записаться к психологу.
6. В конце сообщения с предложением записи добавь: "ЗАПИСЬ_ГОТОВА"

Отвечай коротко (2-4 предложения) на русском."""

def get_history(user_id: int):
    if user_id not in user_history:
        user_history[user_id] = [{"role": "system", "content": SYSTEM_PROMPT}]
    return user_history[user_id]

def detect_direction(text: str) -> str:
    text_lower = text.lower()
    keywords = {
        "тревога": ["тревог", "страх", "паник", "боюсь"],
        "отношения": ["отношени", "партнёр", "муж", "жена", "ссор"],
        "выгорание": ["выгоран", "устал", "нет сил", "апати"],
        "самооценка": ["самооценк", "неуверен", "комплекс"],
        "дети": ["ребёнк", "дочь", "сын", "родител"]
    }
    for direction, words in keywords.items():
        for word in words:
            if word in text_lower:
                return direction
    return "общая поддержка"

# ========== УВЕДОМЛЕНИЕ ПСИХОЛОГУ ==========
async def notify_psychologist(user_id: int, username: str, problem: str, direction: str, contact: str):
    message = f"🔔 **НОВЫЙ ЗАПРОС**\n\n👤 {username}\n📝 {problem[:300]}\n🏷 {direction}\n📞 {contact}"
    if PSYCHOLOGIST_ID:
        try:
            await bot.send_message(PSYCHOLOGIST_ID, message, parse_mode="Markdown")
        except Exception as e:
            logging.error(f"Ошибка: {e}")

# ========== СОХРАНЕНИЕ В GOOGLE SHEETS ==========
def save_to_google_sheets(user_id: int, username: str, problem: str, direction: str, contact: str):
    """Сохраняет заявку в Google Sheets"""
    try:
        if not GOOGLE_CREDENTIALS_JSON or not SHEET_ID:
            print("⚠️ GOOGLE_CREDENTIALS_JSON или SHEET_ID не найдены")
            return False
        
        # Московское время (UTC+3)
        moscow_time = datetime.now() + timedelta(hours=3)
        
        creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
        creds = Credentials.from_service_account_info(
            creds_dict,
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        client = gspread.authorize(creds)
        sheet = client.open_by_key(SHEET_ID).sheet1
        
        if not sheet.get_all_values():
            headers = ["Timestamp", "User ID", "Username", "Problem", "Direction", "Contact", "Status"]
            sheet.append_row(headers)
        
        row = [
            moscow_time.strftime("%Y-%m-%d %H:%M:%S"),
            user_id,
            username,
            problem[:200],
            direction,
            contact,
            "new"
        ]
        sheet.append_row(row)
        print(f"✅ Заявка сохранена в Google Sheets: {username}")
        return True
    except Exception as e:
        print(f"❌ Ошибка сохранения в Google Sheets: {e}")
        return False

# ========== ОСНОВНЫЕ ОБРАБОТЧИКИ ==========
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    await state.clear()
    if user_id in user_history:
        del user_history[user_id]
    if user_id in user_problems:
        del user_problems[user_id]
    
    await message.answer(
        f"👋 **Привет! Я {PSYCHOLOGIST_NAME}, психолог-ассистент.**\n\n"
        f"Расскажите, что вас беспокоит. Я внимательно выслушаю.\n\n"
        f"Также я могу:\n"
        f"🔮 **Число судьбы** - по дате рождения\n"
        f"⭐ **Гороскоп** - прогноз на сегодня\n"
        f"♊ **Совместимость** - анализ пары\n"
        f"🎴 **Карта дня Таро** - мудрость древних\n\n"
        f"👇 Используйте кнопки меню",
        reply_markup=menu_keyboard,
        parse_mode="Markdown"
    )
    await state.set_state(Dialogue.chatting)

@dp.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Отменено.", reply_markup=menu_keyboard)
    await state.set_state(Dialogue.chatting)

@dp.message(Command("reset"))
async def cmd_reset(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    if user_id in user_history:
        del user_history[user_id]
    if user_id in user_problems:
        del user_problems[user_id]
    await state.clear()
    await message.answer("🔄 История очищена.", reply_markup=menu_keyboard)
    await state.set_state(Dialogue.chatting)

# ========== КНОПКИ МЕНЮ ==========
@dp.message(F.text == "ℹ️ Помощь")
async def menu_help(message: types.Message):
    await message.answer(
        "📖 **Доступные функции:**\n\n"
        "💬 **Просто напишите** - психологическая поддержка\n"
        "🔮 **Число судьбы** - расчет по дате рождения\n"
        "⭐ **Гороскоп** - прогноз на сегодня\n"
        "♊ **Совместимость** - анализ пары\n"
        "🎴 **Карта дня Таро** - предсказание\n"
        "📞 **Запись к психологу** - живая консультация\n\n"
        "🗑 /reset - очистить диалог\n"
        "❌ /cancel - отмена",
        reply_markup=menu_keyboard
    )

@dp.message(F.text == "🗑 Очистить диалог")
async def menu_reset(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    if user_id in user_history:
        del user_history[user_id]
    if user_id in user_problems:
        del user_problems[user_id]
    await state.clear()
    await message.answer("🗑 История очищена.", reply_markup=menu_keyboard)
    await state.set_state(Dialogue.chatting)

@dp.message(F.text == "🔮 Число судьбы")
async def fate_number_start(message: types.Message, state: FSMContext):
    await state.set_state(Dialogue.waiting_for_birthdate)
    await message.answer(
        "🔮 **Расчет числа судьбы**\n\nВведите дату рождения:\n`ДД.ММ.ГГГГ`\nПример: 15.05.1990",
        parse_mode="Markdown"
    )

@dp.message(F.text == "⭐ Гороскоп")
async def horoscope_start(message: types.Message, state: FSMContext):
    await state.set_state(Dialogue.waiting_for_zodiac)
    await message.answer(
        "⭐ **Гороскоп**\n\nВведите ваш знак зодиака или дату рождения:\n"
        "Овен, Телец, Близнецы, Рак, Лев, Дева, Весы, Скорпион, Стрелец, Козерог, Водолей, Рыбы\n\n"
        "Или дату в формате `ДД.ММ.ГГГГ`",
        parse_mode="Markdown"
    )

@dp.message(F.text == "♊ Совместимость")
async def compatibility_start(message: types.Message, state: FSMContext):
    await state.set_state(Dialogue.waiting_for_birthdate_comp)
    await message.answer(
        "♊ **Расчет совместимости**\n\nВведите **первую** дату рождения:\n`ДД.ММ.ГГГГ`",
        parse_mode="Markdown"
    )

@dp.message(F.text == "🎴 Карта дня Таро")
async def taro_card_handler(message: types.Message):
    taro_cards = {
        "Шут": "🎭 Новое начало, спонтанность. Позвольте себе сделать первый шаг!",
        "Маг": "🪄 Сила воли, проявление желаний. У вас есть все ресурсы!",
        "Верховная Жрица": "🌙 Интуиция, тайны. Доверьтесь своему внутреннему голосу.",
        "Императрица": "👑 Творчество, изобилие. Пришло время творить.",
        "Император": "🏛️ Структура, власть. Укрепляйте свои границы.",
        "Иерофант": "⛪ Традиции, обучение. Обратитесь к опыту старших.",
        "Влюбленные": "💕 Любовь, выбор. Важный выбор на пути.",
        "Колесница": "⚡ Воля, победа. Управляйте своей судьбой!",
        "Сила": "🦁 Мужество, сила. Вы сильнее, чем кажетесь.",
        "Отшельник": "🏮 Самоанализ, мудрость. Время побыть наедине.",
        "Колесо Фортуны": "🎡 Перемены, удача. Жизнь меняется к лучшему.",
        "Справедливость": "⚖️ Честность, закон. Поступите справедливо.",
        "Повешенный": "🪢 Новая перспектива. Посмотрите на ситуацию иначе.",
        "Смерть": "♻️ Трансформация. Старое уходит, новое приходит.",
        "Умеренность": "⚖️ Баланс, терпение. Найдите золотую середину.",
        "Дьявол": "😈 Освобождение. От чего пора отказаться?",
        "Башня": "🏛️💥 Внезапные перемены. Крах иллюзий.",
        "Звезда": "⭐ Надежда, исцеление. Верьте в лучшее!",
        "Луна": "🌕 Иллюзии, страхи. Доверяйте интуиции.",
        "Солнце": "☀️ Радость, успех. Всё будет хорошо!",
        "Суд": "🎺 Пробуждение, прощение. Время подвести итоги.",
        "Мир": "🌍 Завершение, удовлетворение. Вы достигли цели!"
    }
    
    card_name = random.choice(list(taro_cards.keys()))
    card_meaning = taro_cards[card_name]
    
    await message.answer(
        f"🎴 **Карта дня: {card_name}**\n\n{card_meaning}\n\n"
        f"✨ Прислушайтесь к её посланию сегодня.",
        parse_mode="Markdown",
        reply_markup=menu_keyboard
    )

@dp.message(F.text == "📞 Запись к психологу")
async def book_psychologist(message: types.Message, state: FSMContext):
    await message.answer(
        "📝 **Запись на консультацию**\n\n"
        "Оставьте ваш контакт (@username или телефон), и я передам его психологу.\n\n"
        "Или нажмите /cancel для отмены.",
        reply_markup=ReplyKeyboardRemove()
    )
    await state.set_state(Dialogue.waiting_for_contact)

# ========== ВВОД ДАННЫХ ДЛЯ РАСЧЕТОВ ==========
@dp.message(StateFilter(Dialogue.waiting_for_birthdate))
async def process_fate_number(message: types.Message, state: FSMContext):
    if not re.match(r'^\d{2}\.\d{2}\.\d{4}$', message.text):
        await message.answer("❌ Неверный формат. Введите как `ДД.ММ.ГГГГ`", reply_markup=menu_keyboard)
        return
    
    number, description = calculate_fate_number(message.text)
    await message.answer(
        f"🔮 **Ваше число судьбы: {number}**\n\n{description}",
        parse_mode="Markdown",
        reply_markup=menu_keyboard
    )
    await state.set_state(Dialogue.chatting)

@dp.message(StateFilter(Dialogue.waiting_for_zodiac))
async def process_horoscope(message: types.Message, state: FSMContext):
    text = message.text.strip()
    zodiac_sign = None
    
    if re.match(r'^\d{2}\.\d{2}\.\d{4}$', text):
        day, month, _ = map(int, text.split('.'))
        zodiac_sign = get_zodiac_sign(day, month)
        await message.answer(f"♈ Ваш знак: **{zodiac_sign}**")
    else:
        known = ["овен","телец","близнецы","рак","лев","дева","весы","скорпион","стрелец","козерог","водолей","рыбы"]
        for sign in known:
            if text.lower() == sign:
                zodiac_sign = sign.capitalize()
                break
        if not zodiac_sign:
            await message.answer("❌ Неизвестный знак. Попробуйте еще раз.", reply_markup=menu_keyboard)
            return
    
    forecasts = {
        "Овен": "🔥 Энергия бьет ключом! Начните новые дела!",
        "Телец": "💰 Хороший день для финансовых решений.",
        "Близнецы": "💬 День общения и новых знакомств.",
        "Рак": "🏠 День интуиции и семьи.",
        "Лев": "🎭 Творческий день. Покажите себя!",
        "Дева": "📋 День порядка и планирования.",
        "Весы": "⚖️ День гармонии. Избегайте конфликтов.",
        "Скорпион": "🦂 День трансформации и глубоких мыслей.",
        "Стрелец": "✈️ День приключений и оптимизма.",
        "Козерог": "🏔️ День достижений. Будьте упорны.",
        "Водолей": "💡 День идей и нестандартных решений.",
        "Рыбы": "🎨 День творчества и интуиции."
    }
    forecast = forecasts.get(zodiac_sign, "🌟 Гармоничный день.")
    await message.answer(
        f"✨ **Гороскоп для {zodiac_sign}** ✨\n\n📅 {forecast}",
        parse_mode="Markdown",
        reply_markup=menu_keyboard
    )
    await state.set_state(Dialogue.chatting)

@dp.message(StateFilter(Dialogue.waiting_for_birthdate_comp))
async def process_compatibility_first(message: types.Message, state: FSMContext):
    if not re.match(r'^\d{2}\.\d{2}\.\d{4}$', message.text):
        await message.answer("❌ Неверный формат. Введите как `ДД.ММ.ГГГГ`", reply_markup=menu_keyboard)
        return
    await state.update_data(date1=message.text)
    await state.set_state(Dialogue.waiting_for_birthdate_comp2)
    await message.answer("Введите **вторую** дату рождения:\n`ДД.ММ.ГГГГ`", parse_mode="Markdown")

@dp.message(StateFilter(Dialogue.waiting_for_birthdate_comp2))
async def process_compatibility_second(message: types.Message, state: FSMContext):
    if not re.match(r'^\d{2}\.\d{2}\.\d{4}$', message.text):
        await message.answer("❌ Неверный формат. Введите как `ДД.ММ.ГГГГ`", reply_markup=menu_keyboard)
        return
    
    data = await state.get_data()
    date1 = data.get('date1')
    if not date1:
        await message.answer("❌ Ошибка. Начните заново.", reply_markup=menu_keyboard)
        await state.clear()
        return
    
    result = get_compatibility(date1, message.text)
    if result['percent'] == 0:
        await message.answer(f"❌ {result['text']}", reply_markup=menu_keyboard)
    else:
        await message.answer(
            f"♊ **Совместимость**\n\n"
            f"📅 {date1} -> {result['sign1']}\n"
            f"📅 {message.text} -> {result['sign2']}\n\n"
            f"💕 **{result['percent']}%**\n{result['text']}",
            parse_mode="Markdown",
            reply_markup=menu_keyboard
        )
    await state.set_state(Dialogue.chatting)

# ========== ОБРАБОТКА ЗАПИСИ К ПСИХОЛОГУ (С СОХРАНЕНИЕМ В GOOGLE SHEETS) ==========
@dp.message(StateFilter(Dialogue.waiting_for_contact))
async def process_contact(message: types.Message, state: FSMContext):
    contact = message.text
    user_id = message.from_user.id
    username = message.from_user.username or "None"
    problem_info = user_problems.get(user_id, {"problem": "Диалог с ИИ", "direction": "не определено"})
    
    # Сохраняем в Google Sheets
    save_to_google_sheets(user_id, username, problem_info["problem"], problem_info["direction"], contact)
    
    # Отправляем уведомление психологу
    await notify_psychologist(user_id, username, problem_info["problem"], problem_info["direction"], contact)
    
    if user_id in user_history:
        del user_history[user_id]
    if user_id in user_problems:
        del user_problems[user_id]
    
    await message.answer(
        f"✅ Спасибо! Психолог {PSYCHOLOGIST_NAME} свяжется с вами.\n\nБерегите себя ❤️",
        reply_markup=menu_keyboard
    )
    await state.clear()
    await state.set_state(Dialogue.chatting)

# ========== ОСНОВНОЙ ДИАЛОГ С ИИ ==========
@dp.message(Dialogue.chatting)
async def chat_with_ai(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    user_text = message.text
    
    # Пропускаем кнопки меню
    menu_buttons = ["ℹ️ Помощь", "🗑 Очистить диалог", "🔮 Число судьбы", 
                    "⭐ Гороскоп", "♊ Совместимость", "🎴 Карта дня Таро", 
                    "📞 Запись к психологу"]
    if user_text in menu_buttons:
        return
    
    print(f"📨 Получено: {user_text}")
    
    # Кризисная проверка
    crisis = ["суицид", "самоубийств", "не хочу жить", "покончить с собой"]
    if any(word in user_text.lower() for word in crisis):
        await message.answer("🚨 Телефон доверия: 8-800-2000-122. Пожалуйста, позвоните ❤️")
        return
    
    if not groq_client:
        await message.answer("🤖 ИИ-ассистент временно недоступен. Используйте кнопки меню.", reply_markup=menu_keyboard)
        return
    
    if user_id not in user_problems:
        user_problems[user_id] = {"problem": user_text, "direction": detect_direction(user_text)}
    
    try:
        history = get_history(user_id)
        history.append({"role": "user", "content": user_text})
        
        response = await groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=history,
            max_tokens=350,
            temperature=0.8
        )
        
        answer = response.choices[0].message.content
        history.append({"role": "assistant", "content": answer})
        
        if len(history) > 15:
            user_history[user_id] = [history[0]] + history[-12:]
        else:
            user_history[user_id] = history
        
        if "ЗАПИСЬ_ГОТОВА" in answer:
            answer = answer.replace("ЗАПИСЬ_ГОТОВА", "").strip()
            if answer:
                await message.answer(answer)
            
            book_keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="📝 Записаться", callback_data="book")],
                    [InlineKeyboardButton(text="❌ Отмена", callback_data="not_ready")]
                ]
            )
            await message.answer(
                f"💬 **Как вы смотрите на то, чтобы обсудить это с психологом {PSYCHOLOGIST_NAME}?**",
                reply_markup=book_keyboard
            )
        else:
            await message.answer(answer)
        
    except Exception as e:
        print(f"❌ Ошибка ИИ: {e}")
        await message.answer("Извините, произошла ошибка. Попробуйте ещё раз.", reply_markup=menu_keyboard)

# ========== КОЛБЭКИ ДЛЯ ИНЛАЙН-КНОПОК ==========
@dp.callback_query(lambda c: c.data == "book")
async def handle_book(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.answer(
        "📝 **Оставьте контакт**\n\nНапишите @username или номер телефона.\n\n"
        "Или нажмите /cancel для отмены.",
        reply_markup=ReplyKeyboardRemove()
    )
    await state.set_state(Dialogue.waiting_for_contact)

@dp.callback_query(lambda c: c.data == "not_ready")
async def handle_not_ready(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await callback.message.answer("❌ Отменено.", reply_markup=menu_keyboard)
    await state.set_state(Dialogue.chatting)

# ========== ЗАПУСК ==========
async def main():
    print("🚀 Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
