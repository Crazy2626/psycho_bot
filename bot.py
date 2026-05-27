import asyncio
import logging
import os
import re
import random
import json
import sqlite3
from datetime import datetime, timedelta
from contextlib import contextmanager

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardRemove, LabeledPrice, PreCheckoutQuery
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
SUBSCRIPTION_PRICE = 99  # 99 Stars
FREE_QUESTIONS_PER_DAY = 7

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ========== БАЗА ДАННЫХ SQLITE ==========
DB_PATH = "bot_data.db"

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def init_db():
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                gender TEXT,
                name TEXT,
                registered_at TIMESTAMP,
                is_premium BOOLEAN DEFAULT 0,
                premium_until TIMESTAMP
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS message_counts (
                user_id INTEGER PRIMARY KEY,
                count INTEGER DEFAULT 0,
                last_reset_date TEXT
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_birthdates (
                user_id INTEGER PRIMARY KEY,
                birth_date TEXT
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS daily_forecasts (
                user_id INTEGER PRIMARY KEY,
                last_forecast_date TEXT,
                forecast_time TEXT
            )
        ''')

init_db()

# ========== FSM СОСТОЯНИЯ ==========
class Dialogue(StatesGroup):
    choosing_gender = State()
    chatting = State()
    waiting_for_contact = State()
    waiting_for_birthdate = State()
    waiting_for_birthdate_comp = State()
    waiting_for_birthdate_comp2 = State()
    waiting_for_zodiac = State()

# ========== КЛАВИАТУРЫ ==========
menu_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="ℹ️ Помощь"), KeyboardButton(text="🗑 Очистить диалог")],
        [KeyboardButton(text="🔮 Число судьбы"), KeyboardButton(text="⭐ Гороскоп")],
        [KeyboardButton(text="♊ Совместимость"), KeyboardButton(text="🎴 Карта дня Таро")],
        [KeyboardButton(text="📞 Запись к психологу"), KeyboardButton(text="⭐ Подписка Premium")],
        [KeyboardButton(text="📊 Демо-отчёт")]  # НОВАЯ КНОПКА
    ],
    resize_keyboard=True
)

gender_keyboard = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="👩 Женский"), KeyboardButton(text="👨 Мужской")]],
    resize_keyboard=True,
    one_time_keyboard=True
)

premium_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="💎 Оформить подписку 99 Stars/мес", callback_data="buy_subscription")],
        [InlineKeyboardButton(text="🔍 Что даёт подписка?", callback_data="what_is_premium")]
    ]
)

# ========== РАБОТА С БАЗОЙ ДАННЫХ ==========
def get_user_gender(user_id: int) -> str:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT gender FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        return row[0] if row and row[0] else "female"

def get_user_name(user_id: int) -> str:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        return row[0] if row else "друг"

def get_user_birthdate(user_id: int) -> str:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT birth_date FROM user_birthdates WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        return row[0] if row else None

def save_user_birthdate(user_id: int, birth_date: str):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO user_birthdates (user_id, birth_date) VALUES (?, ?)", (user_id, birth_date))

def set_user_gender(user_id: int, username: str, gender: str, name: str):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO users (user_id, username, gender, name, registered_at)
            VALUES (?, ?, ?, ?, ?)
        ''', (user_id, username, gender, name, datetime.now()))

def is_premium(user_id: int) -> bool:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT is_premium, premium_until FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if row and row[0] and row[1]:
            premium_until = datetime.fromisoformat(row[1])
            if premium_until > datetime.now():
                return True
        return False

def get_remaining_questions(user_id: int) -> int:
    if is_premium(user_id):
        return 999
    today_str = datetime.now().strftime("%Y-%m-%d")
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT count, last_reset_date FROM message_counts WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if row:
            count, last_reset = row
            if last_reset != today_str:
                count = 0
                cursor.execute("UPDATE message_counts SET count = ?, last_reset_date = ? WHERE user_id = ?",
                               (count, today_str, user_id))
        else:
            count = 0
            cursor.execute("INSERT INTO message_counts (user_id, count, last_reset_date) VALUES (?, ?, ?)",
                           (user_id, count, today_str))
    return FREE_QUESTIONS_PER_DAY - count

def increment_question_count(user_id: int) -> int:
    if is_premium(user_id):
        return FREE_QUESTIONS_PER_DAY
    today_str = datetime.now().strftime("%Y-%m-%d")
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT count, last_reset_date FROM message_counts WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if row:
            count, last_reset = row
            if last_reset != today_str:
                count = 0
            count += 1
            cursor.execute("UPDATE message_counts SET count = ?, last_reset_date = ? WHERE user_id = ?",
                           (count, today_str, user_id))
        else:
            count = 1
            cursor.execute("INSERT INTO message_counts (user_id, count, last_reset_date) VALUES (?, ?, ?)",
                           (user_id, count, today_str))
    return FREE_QUESTIONS_PER_DAY - count

def activate_premium(user_id: int, duration_days: int = 30):
    until = datetime.now() + timedelta(days=duration_days)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET is_premium = 1, premium_until = ? WHERE user_id = ?",
                       (until.isoformat(), user_id))

# ========== ПРАВИЛЬНОЕ ОПРЕДЕЛЕНИЕ ЗНАКОВ ЗОДИАКА ==========
def get_zodiac_sign(day: int, month: int) -> str:
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

def calculate_fate_number(birth_date: str, gender: str = "female") -> tuple:
    try:
        day, month, year = map(int, birth_date.split('.'))
        total = day + month + year
        while total > 9:
            total = sum(int(d) for d in str(total))
        descriptions = {
            1: ("🔴 **1 — Число Лидера**\n\nТы — прирождённый первопроходец! Твоя миссия — начинать новое и вдохновлять других. Ты независим" + ("а" if gender == "female" else "") + ", амбициозен" + ("на" if gender == "female" else "") + " и полон" + ("а" if gender == "female" else "") + " идей. \n\n✨ **Твой путь:** самостоятельность и смелость.\n💫 **Твой талант:** ты видишь то, что другие не замечают.\n🌟 **Совет:** доверяй своей интуиции и не бойся быть первым" + ("ой" if gender == "female" else "") + "!"),
            2: "🟠 **2 — Число Миротворца**\n\nТы — душа любой компании, дипломат и миротворец. Твоя суперсила — создавать гармонию там, где хаос. \n\n✨ **Твой путь:** сотрудничество и понимание.\n💫 **Твой талант:** ты чувствуешь эмоции других.\n🌟 **Совет:** не забывай о своих желаниях, заботясь о других!",
            3: "🟡 **3 — Число Творца**\n\nТы — источник радости и вдохновения! Твоя энергия заражает всех вокруг. \n\n✨ **Твой путь:** самовыражение и творчество.\n💫 **Твой талант:** ты легко находишь слова и идеи.\n🌟 **Совет:** не бойся быть в центре внимания — это твоя стихия!",
            4: "🟢 **4 — Число Строителя**\n\nТы — надёжная опора для всех. Твоя сила в дисциплине и упорстве. \n\n✨ **Твой путь:** создание прочных основ.\n💫 **Твой талант:** ты доводишь дела до конца.\n🌟 **Совет:** иногда позволяй себе отдыхать и не будь слишком строг" + ("ой" if gender == "female" else "им") + " к себе!",
            5: "🔵 **5 — Число Свободы**\n\nТы — искатель приключений! Перемены — твой двигатель, рутина — твой враг. \n\n✨ **Твой путь:** свобода и новые впечатления.\n💫 **Твой талант:** ты легко адаптируешься к любому.\n🌟 **Совет:** наслаждайся путешествиями и новыми знакомствами!",
            6: "🔵 **6 — Число Заботы**\n\nТы — сердце семьи и опора для близких. Твоя любовь безусловна. \n\n✨ **Твой путь:** забота и ответственность.\n💫 **Твой талант:** ты создаёшь уют и гармонию.\n🌟 **Совет:** не забывай заботиться и о себе!",
            7: "🟣 **7 — Число Мудрости**\n\nТы — исследователь глубин. Тебе нужно время для размышлений и одиночества. \n\n✨ **Твой путь:** познание и мудрость.\n💫 **Твой талант:** ты видишь то, что скрыто от других.\n🌟 **Совет:** доверяй своей интуиции — она редко ошибается!",
            8: "⚫️ **8 — Число Силы**\n\nТы — рождённ" + ("ая" if gender == "female" else "ый") + " для успеха! Деньги и власть приходят к тебе, когда ты в гармонии с собой. \n\n✨ **Твой путь:** материальная реализация.\n💫 **Твой талант:** ты умеешь зарабатывать и управлять.\n🌟 **Совет:** не забывай о духовном развитии!",
            9: "⚪️ **9 — Число Завершения**\n\nТы — гуманист и учитель. Твоя миссия — помогать другим и завершать старое, открывая путь новому. \n\n✨ **Твой путь:** служение людям.\n💫 **Твой талант:** ты чувствуешь боль других и хочешь помочь.\n🌟 **Совет:** научись прощать и отпускать — это твой ключ к счастью!"
        }
        return (total, descriptions.get(total, "✨ Уникальная личность с особенным путём!"))
    except:
        return (0, "❌ Ошибка формата даты. Пожалуйста, используй ДД.ММ.ГГГГ")

def get_compatibility(date1: str, date2: str, premium: bool = False) -> dict:
    try:
        day1, month1, _ = map(int, date1.split('.'))
        day2, month2, _ = map(int, date2.split('.'))
        sign1 = get_zodiac_sign(day1, month1)
        sign2 = get_zodiac_sign(day2, month2)
        elements = {"Овен": "Огонь 🔥", "Лев": "Огонь 🔥", "Стрелец": "Огонь 🔥",
                    "Телец": "Земля 🌍", "Дева": "Земля 🌍", "Козерог": "Земля 🌍",
                    "Близнецы": "Воздух 💨", "Весы": "Воздух 💨", "Водолей": "Воздух 💨",
                    "Рак": "Вода 💧", "Скорпион": "Вода 💧", "Рыбы": "Вода 💧"}
        elem1 = elements.get(sign1, "")
        elem2 = elements.get(sign2, "")
        if elem1 == elem2:
            compatibility = random.randint(85, 98)
            base_text = f"🌟 **Идеальный союз!** Вы принадлежите к одной стихии {elem1}, поэтому понимаете друг друга с полуслова."
        elif (elem1 in ["Огонь 🔥", "Воздух 💨"]) and (elem2 in ["Огонь 🔥", "Воздух 💨"]):
            compatibility = random.randint(75, 90)
            base_text = f"💫 **Яркая пара!** {elem1} + {elem2} = взрывная смесь страсти и свободы."
        elif (elem1 in ["Земля 🌍", "Вода 💧"]) and (elem2 in ["Земля 🌍", "Вода 💧"]):
            compatibility = random.randint(80, 95)
            base_text = f"🌱 **Гармоничный союз!** {elem1} и {elem2} создают плодородную почву для любви."
        else:
            compatibility = random.randint(50, 70)
            base_text = f"🦋 **Загадочный союз.** Вы очень разные, но именно это делает вашу пару уникальной."
        if premium:
            additional = f"\n\n✨ **Развёрнутый анализ Premium:**\n• Сильные стороны: взаимное вдохновение, страсть, интерес\n• Точки роста: учитесь терпению и принятию различий\n• Кармическая задача: построить крепкий союз на основе взаимного уважения"
        else:
            additional = f"\n\n🔓 **Полный разбор совместимости доступен по подписке Premium** (99 ₽/мес):\n• Сильные и слабые стороны пары\n• Кармическая задача\n• Прогноз развития отношений"
        return {"percent": compatibility, "text": base_text + additional, "sign1": sign1, "sign2": sign2}
    except:
        return {"percent": 0, "text": "❌ Ошибка формата даты"}

# ========== ДЕМО-ОТЧЁТ ==========
async def generate_demo_report(user_id: int, birth_date: str = None) -> str:
    """Генерирует демо-отчёт на основе данных пользователя или примерных данных"""
    gender = get_user_gender(user_id)
    name = get_user_name(user_id)
    pronoun = "девушка" if gender == "female" else "парень"
    
    if not birth_date:
        # Если даты нет, используем примерную
        birth_date = "15.06.1990"
        is_demo = True
    else:
        is_demo = False
    
    fate_number, fate_desc = calculate_fate_number(birth_date, gender)
    sign = get_zodiac_sign(int(birth_date.split('.')[0]), int(birth_date.split('.')[1]))
    
    report = f"""
📄 **ПЕРСОНАЛЬНЫЙ ОТЧЁТ «ТВОЯ СУДЬБА»**
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🔮 **ЧИСЛО СУДЬБЫ — {fate_number}**
{fate_desc[:200]}...

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

⭐ **ГОРОСКОП НА {datetime.now().year} ГОД**

Общие тенденции:
Этот год несёт энергию перемен и новых возможностей для вас, {pronoun} {name}.

Любовь ♥️:
Весна и осень — ключевые периоды для отношений.

Карьера 💼:
Лето — время для активных действий и новых проектов.

Финансы 💰:
Осень принесёт неожиданные доходы.

Здоровье 🌿:
Уделите внимание режиму сна и отдыху весной.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

💕 **СОВМЕСТИМОСТЬ С ПАРТНЁРОМ** (пример)

Пример совместимости для {sign} с Весами: 86%

Сильные стороны: взаимное вдохновение, интеллектуальная связь
Точки роста: учитесь принимать различия

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🎴 **РАСКЛАД ТАРО «ПУТЬ ГОДА»**

Карта 1 — Вы сейчас: Маг 🪄
«У вас есть всё для нового этапа»

Карта 2 — Что вас ждёт: Колесница ⚡
«Время действовать и побеждать»

Карта 3 — Испытания: Звезда ⭐
«Надежда поможет преодолеть трудности»

Карта 4 — Итог года: Мир 🌍
«Завершение цикла, достижение цели»

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

✨ **ЕЖЕДНЕВНЫЕ АФФИРМАЦИИ (пример)**

«Я открыта новым возможностям. Вселенная заботится обо мне»
«Мои таланты признаны и ценны»
«Я привлекаю успех и изобилие»

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
    
    if is_demo:
        report += f"\n\n💎 **Это пример отчёта!**\n\n📊 Чтобы получить точный отчёт на основе вашей даты рождения, нажмите /demo_report"
    else:
        report += f"\n\n✨ **Это ваш персональный отчёт на основе вашей даты рождения {birth_date}!**\n\n📊 Полная версия отчёта доступна по подписке Premium (99 Stars/мес)."
    
    return report

@dp.message(F.text == "📊 Демо-отчёт")
async def show_demo_report(message: types.Message):
    user_id = message.from_user.id
    birth_date = get_user_birthdate(user_id)
    
    if not birth_date:
        await message.answer(
            "📊 **Демо-отчёт**\n\n"
            "Чтобы увидеть персонализированный демо-отчёт, сначала введите вашу дату рождения.\n\n"
            "🔮 Нажмите на кнопку «Число судьбы» и введите дату, например: 15.06.1990\n\n"
            "После этого я смогу показать вам пример отчёта на основе ваших данных!",
            reply_markup=menu_keyboard
        )
        return
    
    await message.answer("📊 Генерирую ваш персональный демо-отчёт... Подождите немного ✨")
    
    report = await generate_demo_report(user_id, birth_date)
    
    await message.answer(
        report,
        parse_mode="Markdown",
        reply_markup=menu_keyboard
    )
    
    await message.answer(
        "💎 **Хотите полную версию?**\n\n"
        "В Premium-отчёте вы получите:\n"
        "✅ 25+ страниц с персональными прогнозами\n"
        "✅ Разбор 5 сфер: любовь, деньги, карьера, здоровье, саморазвитие\n"
        "✅ Детальный анализ совместимости (12 страниц)\n"
        "✅ Ежедневные аффирмации на месяц\n"
        "✅ PDF-файл для печати\n\n"
        "⭐ Всего за 99 Stars/мес!\n\n"
        "👉 Нажмите «⭐ Подписка Premium» в меню, чтобы оформить.",
        reply_markup=premium_keyboard
    )

@dp.message(Command("demo_report"))
async def cmd_demo_report(message: types.Message):
    await show_demo_report(message)

# ========== ХРАНИЛИЩЕ ИСТОРИИ ДИАЛОГОВ ==========
user_history = {}
user_problems = {}

def get_system_prompt(gender: str, name: str) -> str:
    pronoun = "девушка" if gender == "female" else "парень"
    return f"""Ты — эмпатичный психолог-помощник по имени {PSYCHOLOGIST_NAME}.

Пользователь — {pronoun} по имени {name}. Обращайся к нему/ней соответственно (используй окончания "а" для женщин, "ил" для мужчин).

Правила:
1. Внимательно слушай и задавай вопросы.
2. Проявляй эмпатию.
3. Не ставь диагнозы.
4. При кризисе — дай телефон доверия: 8-800-2000-122.
5. После 4-6 обменов мягко предложи записаться к психологу.
6. В конце сообщения с предложением записи добавь: "ЗАПИСЬ_ГОТОВА"

Отвечай коротко (2-4 предложения) на русском, используя правильные окончания."""

def get_history(user_id: int, gender: str, name: str):
    if user_id not in user_history:
        user_history[user_id] = [{"role": "system", "content": get_system_prompt(gender, name)}]
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

# ========== УВЕДОМЛЕНИЕ ПСИХОЛОГУ И GOOGLE SHEETS ==========
async def notify_psychologist(user_id: int, username: str, problem: str, direction: str, contact: str):
    message = f"🔔 **НОВЫЙ ЗАПРОС**\n\n👤 {username}\n📝 {problem[:300]}\n🏷 {direction}\n📞 {contact}"
    if PSYCHOLOGIST_ID:
        try:
            await bot.send_message(PSYCHOLOGIST_ID, message, parse_mode="Markdown")
        except Exception as e:
            logging.error(f"Ошибка: {e}")

def save_to_google_sheets(user_id: int, username: str, problem: str, direction: str, contact: str):
    try:
        if not GOOGLE_CREDENTIALS_JSON or not SHEET_ID:
            print("⚠️ GOOGLE_CREDENTIALS_JSON или SHEET_ID не найдены")
            return False
        moscow_time = datetime.now() + timedelta(hours=3)
        creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
        creds = Credentials.from_service_account_info(creds_dict, scopes=["https://www.googleapis.com/auth/spreadsheets"])
        client = gspread.authorize(creds)
        sheet = client.open_by_key(SHEET_ID).sheet1
        if not sheet.get_all_values():
            headers = ["Timestamp", "User ID", "Username", "Problem", "Direction", "Contact", "Status"]
            sheet.append_row(headers)
        row = [moscow_time.strftime("%Y-%m-%d %H:%M:%S"), user_id, username, problem[:200], direction, contact, "new"]
        sheet.append_row(row)
        print(f"✅ Заявка сохранена: {username}")
        return True
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        return False

# ========== ПОДПИСКА ==========
@dp.message(F.text == "⭐ Подписка Premium")
async def show_premium_info(message: types.Message):
    user_id = message.from_user.id
    if is_premium(user_id):
        await message.answer(
            "💎 **У вас уже активна Premium-подписка!** 💎\n\nСпасибо, что поддерживаете проект.\n\nДоступны все функции без ограничений ✨",
            reply_markup=menu_keyboard
        )
    else:
        remaining = get_remaining_questions(user_id)
        await message.answer(
            f"⭐ **Premium-подписка 99 Telegram Stars/мес** ⭐\n\n"
            f"📊 **Ваш лимит сегодня:** {remaining}/{FREE_QUESTIONS_PER_DAY} бесплатных вопросов\n\n"
            f"💎 **Что даёт Premium:**\n✅ Безлимитные вопросы к ИИ-психологу\n✅ Расширенные ответы в разделе «Совместимость»\n✅ Персонализированные прогнозы\n✅ Полный PDF-отчёт\n✅ Приоритетную поддержку\n\n"
            f"✨ Нажмите кнопку ниже, чтобы оформить подписку!",
            reply_markup=premium_keyboard
        )

@dp.callback_query(lambda c: c.data == "what_is_premium")
async def what_is_premium(callback: types.CallbackQuery):
    await callback.answer()
    await callback.message.answer(
        "🔮 **Что даёт Premium-подписка?**\n\n"
        "1️⃣ **Безлимитные консультации** с ИИ-психологом\n\n"
        "2️⃣ **Полный разбор совместимости** — детальный анализ пары\n\n"
        "3️⃣ **Расширенные прогнозы** — гороскоп с рекомендациями\n\n"
        "4️⃣ **PDF-отчёт** — персональный документ на 25+ страниц\n\n"
        "5️⃣ **Приоритетная поддержка**\n\n"
        "💎 **Стоимость:** всего 99 Stars (~99 ₽) в месяц.\n\nНажмите «Оформить подписку» ✨"
    )

@dp.callback_query(lambda c: c.data == "buy_subscription")
async def buy_subscription(callback: types.CallbackQuery):
    await callback.answer()
    prices = [LabeledPrice(label="Premium-подписка на месяц", amount=SUBSCRIPTION_PRICE)]
    await callback.message.answer_invoice(
        title="Premium-подписка",
        description="Неограниченные консультации с ИИ-психологом + расширенные функции + PDF-отчёт",
        payload="premium_subscription_30d",
        provider_token="",
        currency="XTR",
        prices=prices,
        start_parameter="premium_start"
    )

@dp.pre_checkout_query()
async def process_pre_checkout(query: PreCheckoutQuery):
    await query.answer(ok=True)

@dp.message(F.successful_payment)
async def process_successful_payment(message: types.Message):
    user_id = message.from_user.id
    activate_premium(user_id, 30)
    await message.answer(
        "💎 **Поздравляем! Premium-подписка активирована!** 💎\n\n"
        "✨ Теперь вам доступны безлимитные консультации и все расширенные функции.\n\n"
        "📊 Нажмите «📊 Демо-отчёт», чтобы увидеть ваш персональный отчёт!\n\n"
        "Спасибо, что поддерживаете проект! 🙏",
        reply_markup=menu_keyboard
    )

# ========== ОБРАБОТЧИК КОМАНДЫ СТАРТ ==========
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    remaining = get_remaining_questions(user_id)
    premium_status = "💎 Premium" if is_premium(user_id) else f"📊 {remaining}/{FREE_QUESTIONS_PER_DAY} вопросов сегодня"
    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT gender FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
    
    if row and row[0]:
        await state.clear()
        if user_id in user_history:
            del user_history[user_id]
        if user_id in user_problems:
            del user_problems[user_id]
        gender = row[0]
        name = message.from_user.first_name or "друг"
        greeting = "возвращайся" if gender == "female" else "возвращайся"
        await message.answer(
            f"✨ **С {greeting}, {name}!** ✨\n\n"
            f"🌸 Я {PSYCHOLOGIST_NAME}, твой персональный гид.\n"
            f"Твой статус: {premium_status}\n\n"
            f"💫 Используй кнопки меню или просто напиши мне!",
            reply_markup=menu_keyboard,
            parse_mode="Markdown"
        )
        await state.set_state(Dialogue.chatting)
        return
    
    await state.set_state(Dialogue.choosing_gender)
    await message.answer(
        f"✨ **Привет, {message.from_user.first_name or 'дорогой друг'}!** ✨\n\n"
        f"🌸 Я {PSYCHOLOGIST_NAME}, твой персональный помощник.\n\n"
        f"💫 Прежде чем мы начнём, скажи, как к тебе обращаться?\n\n👇 **Выбери свой пол:**",
        reply_markup=gender_keyboard,
        parse_mode="Markdown"
    )

@dp.message(StateFilter(Dialogue.choosing_gender))
async def process_gender(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    text = message.text.lower()
    if "жен" in text or text == "👩 женский":
        gender = "female"
        greeting = "👩 Рада знакомству, прекрасная дама!"
    elif "муж" in text or text == "👨 мужской":
        gender = "male"
        greeting = "👨 Рада знакомству, благородный рыцарь!"
    else:
        await message.answer("Пожалуйста, выбери свой пол, нажав на кнопку ниже 👇", reply_markup=gender_keyboard)
        return
    
    set_user_gender(user_id, message.from_user.username or "", gender, message.from_user.first_name or "друг")
    remaining = get_remaining_questions(user_id)
    
    await state.clear()
    await message.answer(
        f"{greeting}\n\n"
        f"🌸 Я {PSYCHOLOGIST_NAME}, твой помощник.\n"
        f"📊 Лимит сегодня: {remaining}/{FREE_QUESTIONS_PER_DAY} вопросов.\n"
        f"⭐ Подписка Premium снимает лимиты и открывает расширенные функции.\n\n"
        f"👇 **Используй кнопки меню или просто напиши мне!**\n\n"
        f"📊 Чтобы увидеть демо-отчёт, сначала укажи дату рождения через кнопку «Число судьбы»!",
        reply_markup=menu_keyboard,
        parse_mode="Markdown"
    )
    await state.set_state(Dialogue.chatting)

# ========== ОСТАЛЬНЫЕ КОМАНДЫ ==========
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
    await message.answer("🧹 История очищена.", reply_markup=menu_keyboard)
    await state.set_state(Dialogue.chatting)

@dp.message(F.text == "ℹ️ Помощь")
async def menu_help(message: types.Message):
    user_id = message.from_user.id
    remaining = get_remaining_questions(user_id)
    await message.answer(
        f"📖 **Что я умею?**\n\n"
        f"📊 Осталось вопросов сегодня: {remaining}/{FREE_QUESTIONS_PER_DAY}\n\n"
        f"💬 **Просто напиши** — я выслушаю и поддержу\n"
        f"🔮 **Число судьбы** — введи дату рождения\n"
        f"⭐ **Гороскоп** — выбери знак или отправь дату\n"
        f"♊ **Совместимость** — введи две даты\n"
        f"🎴 **Карта дня Таро** — мудрый совет\n"
        f"📞 **Запись к психологу** — живая консультация\n"
        f"📊 **Демо-отчёт** — пример полного отчёта\n"
        f"⭐ **Подписка Premium** — безлимитные вопросы + расширенные функции\n\n"
        f"🗑 /reset — начать заново\n❌ /cancel — отмена",
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
    await message.answer("🧹 История очищена.", reply_markup=menu_keyboard)
    await state.set_state(Dialogue.chatting)

@dp.message(F.text == "🔮 Число судьбы")
async def fate_number_start(message: types.Message, state: FSMContext):
    await state.set_state(Dialogue.waiting_for_birthdate)
    await message.answer(
        "🔮 **Расчёт числа судьбы**\n\nВведи свою дату рождения в формате `ДД.ММ.ГГГГ`\n🌙 Например: 15.05.1990",
        parse_mode="Markdown"
    )

@dp.message(StateFilter(Dialogue.waiting_for_birthdate))
async def process_fate_number(message: types.Message, state: FSMContext):
    if not re.match(r'^\d{2}\.\d{2}\.\d{4}$', message.text):
        await message.answer("❌ Неверный формат. Введи как `ДД.ММ.ГГГГ`, например 15.05.1990", reply_markup=menu_keyboard)
        return
    user_id = message.from_user.id
    gender = get_user_gender(user_id)
    birth_date = message.text
    save_user_birthdate(user_id, birth_date)
    number, description = calculate_fate_number(birth_date, gender)
    await message.answer(
        f"🔮 **Твоё число судьбы — {number}** 🔮\n\n{description}\n\n"
        f"📊 Теперь ты можешь посмотреть **демо-отчёт** по кнопке в меню!",
        parse_mode="Markdown",
        reply_markup=menu_keyboard
    )
    await state.set_state(Dialogue.chatting)

@dp.message(F.text == "⭐ Гороскоп")
async def horoscope_start(message: types.Message, state: FSMContext):
    await state.set_state(Dialogue.waiting_for_zodiac)
    await message.answer(
        "⭐ **Гороскоп на сегодня**\n\nВведи знак зодиака или дату рождения:\n"
        "Овен, Телец, Близнецы, Рак, Лев, Дева, Весы, Скорпион, Стрелец, Козерог, Водолей, Рыбы\n\n"
        "✨ Или отправь дату `ДД.ММ.ГГГГ`",
        parse_mode="Markdown"
    )

@dp.message(StateFilter(Dialogue.waiting_for_zodiac))
async def process_horoscope(message: types.Message, state: FSMContext):
    text = message.text.strip()
    zodiac_sign = None
    if re.match(r'^\d{2}\.\d{2}\.\d{4}$', text):
        day, month, _ = map(int, text.split('.'))
        zodiac_sign = get_zodiac_sign(day, month)
        await message.answer(f"♈ **Твой знак: {zodiac_sign}** ♈")
    else:
        known = {"овен":"Овен","телец":"Телец","близнецы":"Близнецы","рак":"Рак","лев":"Лев","дева":"Дева","весы":"Весы","скорпион":"Скорпион","стрелец":"Стрелец","козерог":"Козерог","водолей":"Водолей","рыбы":"Рыбы"}
        if text.lower() in known:
            zodiac_sign = known[text.lower()]
        else:
            await message.answer("❌ Неизвестный знак.", reply_markup=menu_keyboard)
            return
    forecasts = {
        "Овен": "🔥 Энергия бьёт ключом! Начни новые дела!",
        "Телец": "💰 Хороший день для финансовых решений.",
        "Близнецы": "💬 День общения и новых знакомств.",
        "Рак": "🏠 День интуиции и семьи.",
        "Лев": "🎭 Творческий день. Покажи себя!",
        "Дева": "📋 День порядка и планирования.",
        "Весы": "⚖️ День гармонии. Избегай конфликтов.",
        "Скорпион": "🦂 День трансформации.",
        "Стрелец": "✈️ День приключений и оптимизма.",
        "Козерог": "🏔️ День достижений.",
        "Водолей": "💡 День идей и нестандартных решений.",
        "Рыбы": "🎨 День творчества и интуиции."
    }
    forecast = forecasts.get(zodiac_sign, "🌟 Гармоничный день.")
    await message.answer(f"✨ **Гороскоп для {zodiac_sign}** ✨\n\n📅 {forecast}", parse_mode="Markdown", reply_markup=menu_keyboard)
    await state.set_state(Dialogue.chatting)

@dp.message(F.text == "♊ Совместимость")
async def compatibility_start(message: types.Message, state: FSMContext):
    await state.set_state(Dialogue.waiting_for_birthdate_comp)
    await message.answer(
        "💕 **Расчёт совместимости**\n\nВведи **первую** дату рождения в формате `ДД.ММ.ГГГГ`\n🌙 Например: 15.05.1990",
        parse_mode="Markdown"
    )

@dp.message(StateFilter(Dialogue.waiting_for_birthdate_comp))
async def process_compatibility_first(message: types.Message, state: FSMContext):
    if not re.match(r'^\d{2}\.\d{2}\.\d{4}$', message.text):
        await message.answer("❌ Неверный формат. Введи как `ДД.ММ.ГГГГ`", reply_markup=menu_keyboard)
        return
    await state.update_data(date1=message.text)
    await state.set_state(Dialogue.waiting_for_birthdate_comp2)
    await message.answer("💕 Теперь введи **вторую** дату рождения в формате `ДД.ММ.ГГГГ`", parse_mode="Markdown")

@dp.message(StateFilter(Dialogue.waiting_for_birthdate_comp2))
async def process_compatibility_second(message: types.Message, state: FSMContext):
    if not re.match(r'^\d{2}\.\d{2}\.\d{4}$', message.text):
        await message.answer("❌ Неверный формат. Введи как `ДД.ММ.ГГГГ`", reply_markup=menu_keyboard)
        return
    data = await state.get_data()
    date1 = data.get('date1')
    if not date1:
        await message.answer("❌ Ошибка. Начни заново.", reply_markup=menu_keyboard)
        await state.clear()
        return
    user_id = message.from_user.id
    premium = is_premium(user_id)
    result = get_compatibility(date1, message.text, premium)
    if result['percent'] == 0:
        await message.answer(f"❌ {result['text']}", reply_markup=menu_keyboard)
    else:
        await message.answer(
            f"💕 **Результат совместимости** 💕\n\n"
            f"📅 {date1} → **{result['sign1']}**\n"
            f"📅 {message.text} → **{result['sign2']}**\n\n"
            f"🌟 **Совместимость: {result['percent']}%** 🌟\n\n{result['text']}",
            parse_mode="Markdown",
            reply_markup=menu_keyboard
        )
    await state.set_state(Dialogue.chatting)

@dp.message(F.text == "🎴 Карта дня Таро")
async def taro_card_handler(message: types.Message):
    cards = {
        "Шут": "🎭 Новое начало! Пора сделать первый шаг в неизвестность.",
        "Маг": "🪄 У тебя есть все ресурсы для исполнения желаний!",
        "Верховная Жрица": "🌙 Доверься своей интуиции.",
        "Императрица": "👑 Время творить и заботиться.",
        "Император": "🏛️ Укрепляй свои границы.",
        "Иерофант": "⛪ Обратись за советом к старшим.",
        "Влюбленные": "💕 Важный выбор на пути.",
        "Колесница": "⚡ Управляй своей судьбой!",
        "Сила": "🦁 Ты сильнее, чем кажешься.",
        "Отшельник": "🏮 Время побыть наедине.",
        "Колесо Фортуны": "🎡 Жизнь меняется к лучшему.",
        "Справедливость": "⚖️ Поступи справедливо.",
        "Повешенный": "🪢 Посмотри на ситуацию иначе.",
        "Смерть": "♻️ Старое уходит, новое приходит.",
        "Умеренность": "⚖️ Найди золотую середину.",
        "Дьявол": "😈 От чего пора отказаться?",
        "Башня": "🏛️💥 Крах иллюзий, но для нового.",
        "Звезда": "⭐ Верь в лучшее!",
        "Луна": "🌕 Доверяй интуиции.",
        "Солнце": "☀️ Всё будет хорошо!",
        "Суд": "🎺 Время подвести итоги.",
        "Мир": "🌍 Ты достигла цели!"
    }
    card_name = random.choice(list(cards.keys()))
    await message.answer(
        f"🎴 **Твоя карта дня — {card_name}** 🎴\n\n{cards[card_name]}\n\n✨ Пусть этот день принесёт волшебство!",
        parse_mode="Markdown",
        reply_markup=menu_keyboard
    )

@dp.message(F.text == "📞 Запись к психологу")
async def book_psychologist(message: types.Message, state: FSMContext):
    await message.answer(
        "🌸 **Запись на консультацию** 🌸\n\n"
        "Оставь свой контакт (@username или номер телефона), и психолог Дарья свяжется с тобой.\n\n"
        "✨ Всё конфиденциально.\n\nИли нажми /cancel для отмены.",
        reply_markup=ReplyKeyboardRemove()
    )
    await state.set_state(Dialogue.waiting_for_contact)

@dp.message(StateFilter(Dialogue.waiting_for_contact))
async def process_contact(message: types.Message, state: FSMContext):
    contact = message.text
    user_id = message.from_user.id
    username = message.from_user.username or "None"
    problem_info = user_problems.get(user_id, {"problem": "Диалог с ИИ", "direction": "не определено"})
    save_to_google_sheets(user_id, username, problem_info["problem"], problem_info["direction"], contact)
    await notify_psychologist(user_id, username, problem_info["problem"], problem_info["direction"], contact)
    if user_id in user_history:
        del user_history[user_id]
    if user_id in user_problems:
        del user_problems[user_id]
    await message.answer(
        f"🌸 **Спасибо!** Психолог {PSYCHOLOGIST_NAME} свяжется с тобой в ближайшее время.\n\nБереги себя 💕",
        reply_markup=menu_keyboard
    )
    await state.clear()
    await state.set_state(Dialogue.chatting)

# ========== ОСНОВНОЙ ДИАЛОГ С ИИ (С ЛИМИТАМИ) ==========
@dp.message(Dialogue.chatting)
async def chat_with_ai(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    user_text = message.text
    menu_buttons = ["ℹ️ Помощь", "🗑 Очистить диалог", "🔮 Число судьбы", "⭐ Гороскоп", "♊ Совместимость", "🎴 Карта дня Таро", "📞 Запись к психологу", "⭐ Подписка Premium", "📊 Демо-отчёт"]
    if user_text in menu_buttons:
        return
    print(f"📨 Получено: {user_text}")
    crisis = ["суицид", "самоубийств", "не хочу жить", "покончить с собой", "умру"]
    if any(word in user_text.lower() for word in crisis):
        await message.answer("🕊️ Телефон доверия: 8-800-2000-122. Пожалуйста, позвони ❤️")
        return
    remaining = get_remaining_questions(user_id)
    if remaining <= 0 and not is_premium(user_id):
        await message.answer(
            f"📊 **Лимит бесплатных вопросов на сегодня исчерпан** ({FREE_QUESTIONS_PER_DAY}).\n\n"
            f"⭐ Оформите Premium-подписку за 99 Stars/мес, чтобы снять ограничения!\n\n"
            f"👉 Нажмите кнопку «⭐ Подписка Premium» в меню.",
            reply_markup=menu_keyboard
        )
        return
    if user_id not in user_problems:
        user_problems[user_id] = {"problem": user_text, "direction": detect_direction(user_text)}
    try:
        gender = get_user_gender(user_id)
        name = get_user_name(user_id)
        history = get_history(user_id, gender, name)
        history.append({"role": "user", "content": user_text})
        response = await groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=history,
            max_tokens=350,
            temperature=0.9
        )
        answer = response.choices[0].message.content
        history.append({"role": "assistant", "content": answer})
        if len(history) > 15:
            user_history[user_id] = [history[0]] + history[-12:]
        else:
            user_history[user_id] = history
        increment_question_count(user_id)
        new_remaining = get_remaining_questions(user_id)
        if "ЗАПИСЬ_ГОТОВА" in answer:
            answer = answer.replace("ЗАПИСЬ_ГОТОВА", "").strip()
            if answer:
                await message.answer(answer)
            book_keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📝 Да, хочу записаться!", callback_data="book")], [InlineKeyboardButton(text="❌ Пока не готов", callback_data="not_ready")]])
            await message.answer(
                f"💕 **{PSYCHOLOGIST_NAME}** может помочь тебе разобраться в этом глубже.\n\n"
                f"Хочешь обсудить это с живым психологом? Это конфиденциально и не обязывает ни к чему.",
                reply_markup=book_keyboard
            )
        else:
            if not is_premium(user_id):
                answer += f"\n\n📊 Осталось вопросов сегодня: {new_remaining}/{FREE_QUESTIONS_PER_DAY}. ⭐ Подписка Premium снимает лимиты!"
            await message.answer(answer)
    except Exception as e:
        print(f"❌ Ошибка ИИ: {e}")
        await message.answer("🌙 Извини, произошла ошибка. Попробуй ещё раз или используй кнопки меню.", reply_markup=menu_keyboard)

@dp.callback_query(lambda c: c.data == "book")
async def handle_book(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.answer(
        "🌸 **Оставь свой контакт** 🌸\n\nНапиши свой Telegram @username или номер телефона.\nПсихолог Дарья свяжется с тобой.\n\nИли нажми /cancel для отмены.",
        reply_markup=ReplyKeyboardRemove()
    )
    await state.set_state(Dialogue.waiting_for_contact)

@dp.callback_query(lambda c: c.data == "not_ready")
async def handle_not_ready(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await callback.message.answer(
        "🌿 Хорошо, я понимаю. Напиши /start, когда будешь готова 🌸",
        reply_markup=menu_keyboard
    )
    await state.set_state(Dialogue.chatting)

async def main():
    print("✨ Бот с демо-отчётом, подпиской и лимитами запущен! ✨")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
