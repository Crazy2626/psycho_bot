import asyncio
import logging
import os
import re
import random
import json
import sqlite3
import io
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
    ReplyKeyboardRemove, LabeledPrice, PreCheckoutQuery,
    BufferedInputFile
)
from dotenv import load_dotenv
from openai import AsyncOpenAI
import gspread
from google.oauth2.service_account import Credentials
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak
from reportlab.lib.enums import TA_CENTER, TA_LEFT
import pytz

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
SUBSCRIPTION_PRICE = 99
FREE_QUESTIONS_PER_DAY = 7

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# ========== БАЗА ДАННЫХ ==========
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
            CREATE TABLE IF NOT EXISTS daily_forecast_sent (
                user_id INTEGER PRIMARY KEY,
                last_sent_date TEXT
            )
        ''')

init_db()

# ========== FSM ==========
class Dialogue(StatesGroup):
    choosing_gender = State()
    chatting = State()
    waiting_for_contact = State()
    waiting_for_birthdate = State()
    waiting_for_birthdate_comp = State()
    waiting_for_birthdate_comp2 = State()
    waiting_for_zodiac = State()
    waiting_for_partner_date = State()

# ========== КЛАВИАТУРЫ ==========
menu_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="ℹ️ Помощь"), KeyboardButton(text="🗑 Очистить диалог")],
        [KeyboardButton(text="🔮 Число судьбы"), KeyboardButton(text="⭐ Гороскоп")],
        [KeyboardButton(text="♊ Совместимость"), KeyboardButton(text="🎴 Карта дня Таро")],
        [KeyboardButton(text="📞 Запись к психологу"), KeyboardButton(text="⭐ Подписка Premium")],
        [KeyboardButton(text="📊 Демо-отчёт"), KeyboardButton(text="📄 Получить PDF-отчёт")]
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

partner_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="💕 Да, добавить совместимость", callback_data="pdf_with_partner")],
        [InlineKeyboardButton(text="📄 Только мой отчёт", callback_data="pdf_without_partner")]
    ]
)

# ========== ФУНКЦИИ БАЗЫ ДАННЫХ ==========
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

def get_all_premium_users() -> list:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM users WHERE is_premium = 1 AND premium_until > ?", (datetime.now().isoformat(),))
        return [row[0] for row in cursor.fetchall()]

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

def was_forecast_sent_today(user_id: int) -> bool:
    today_str = datetime.now(pytz.timezone('Europe/Moscow')).strftime("%Y-%m-%d")
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT last_sent_date FROM daily_forecast_sent WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        return row and row[0] == today_str

def mark_forecast_sent(user_id: int):
    today_str = datetime.now(pytz.timezone('Europe/Moscow')).strftime("%Y-%m-%d")
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO daily_forecast_sent (user_id, last_sent_date) VALUES (?, ?)",
                       (user_id, today_str))

# ========== ОСНОВНЫЕ ФУНКЦИИ ==========
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
            1: "🔴 **1 — Число Лидера** — Ты прирождённый первопроходец!",
            2: "🟠 **2 — Число Миротворца** — Ты создаёшь гармонию там, где хаос.",
            3: "🟡 **3 — Число Творца** — Твоя энергия заражает всех вокруг.",
            4: "🟢 **4 — Число Строителя** — Твоя сила в дисциплине и упорстве.",
            5: "🔵 **5 — Число Свободы** — Перемены — твой двигатель.",
            6: "🔵 **6 — Число Заботы** — Ты — сердце семьи и опора для близких.",
            7: "🟣 **7 — Число Мудрости** — Ты исследователь глубин.",
            8: "⚫️ **8 — Число Силы** — Ты рождена для успеха!",
            9: "⚪️ **9 — Число Завершения** — Твоя миссия — помогать другим."
        }
        return (total, descriptions.get(total, "✨ Уникальная личность!"))
    except:
        return (0, "❌ Ошибка формата даты")

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
            base_text = f"🌟 Идеальный союз! Вы принадлежите к одной стихии {elem1}."
        elif (elem1 in ["Огонь 🔥", "Воздух 💨"]) and (elem2 in ["Огонь 🔥", "Воздух 💨"]):
            compatibility = random.randint(75, 90)
            base_text = f"💫 Яркая пара! {elem1} + {elem2} = взрывная смесь страсти и свободы."
        elif (elem1 in ["Земля 🌍", "Вода 💧"]) and (elem2 in ["Земля 🌍", "Вода 💧"]):
            compatibility = random.randint(80, 95)
            base_text = f"🌱 Гармоничный союз! {elem1} и {elem2} создают плодородную почву."
        else:
            compatibility = random.randint(50, 70)
            base_text = f"🦋 Загадочный союз. Вы очень разные, но это делает вашу пару уникальной."
        if premium:
            additional = f"\n\n✨ Развёрнутый анализ Premium: сильные стороны, точки роста, кармическая задача."
        else:
            additional = f"\n\n🔓 Полный разбор доступен по подписке Premium (99 ₽/мес)"
        return {"percent": compatibility, "text": base_text + additional, "sign1": sign1, "sign2": sign2}
    except:
        return {"percent": 0, "text": "❌ Ошибка формата даты"}

# ========== PDF-ГЕНЕРАЦИЯ ==========
async def generate_pdf_report(user_id: int, partner_date: str = None) -> io.BytesIO:
    gender = get_user_gender(user_id)
    name = get_user_name(user_id)
    birth_date = get_user_birthdate(user_id)
    if not birth_date:
        birth_date = "01.01.1990"
    
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, topMargin=1.5*cm, bottomMargin=1.5*cm)
    story = []
    
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle('Title', parent=styles['Title'], fontSize=24, alignment=TA_CENTER, spaceAfter=20)
    heading_style = ParagraphStyle('Heading', parent=styles['Heading1'], fontSize=18, textColor='#4A148C', spaceAfter=12)
    normal_style = ParagraphStyle('Normal', parent=styles['Normal'], fontSize=11, leading=14, spaceAfter=6)
    
    story.append(Paragraph(f"<b>Персональный отчёт</b>", title_style))
    story.append(Spacer(1, 0.5*cm))
    story.append(Paragraph(f"<font size=16>для {name}</font>", title_style))
    story.append(Spacer(1, 1*cm))
    story.append(Paragraph(f"📅 Дата рождения: {birth_date}", normal_style))
    story.append(Paragraph(f"📆 Дата составления: {datetime.now().strftime('%d.%m.%Y')}", normal_style))
    story.append(PageBreak())
    
    fate_number, fate_desc = calculate_fate_number(birth_date, gender)
    story.append(Paragraph(f"🔮 Число судьбы — {fate_number}", heading_style))
    story.append(Paragraph(fate_desc, normal_style))
    story.append(Spacer(1, 0.5*cm))
    
    if partner_date:
        comp = get_compatibility(birth_date, partner_date, premium=True)
        story.append(Paragraph(f"💕 Совместимость с партнёром", heading_style))
        story.append(Paragraph(f"📅 {birth_date} → {comp['sign1']}", normal_style))
        story.append(Paragraph(f"📅 {partner_date} → {comp['sign2']}", normal_style))
        story.append(Paragraph(f"🌟 Совместимость: {comp['percent']}%", normal_style))
        story.append(Paragraph(comp['text'], normal_style))
    
    story.append(Paragraph("🌿 Благодарим за доверие!", normal_style))
    
    doc.build(story)
    buffer.seek(0)
    return buffer

# ========== ЕЖЕДНЕВНЫЕ УТРЕННИЕ ОТЧЁТЫ ДЛЯ PREMIUM ==========
async def generate_daily_forecast(user_id: int) -> str:
    gender = get_user_gender(user_id)
    name = get_user_name(user_id)
    birth_date = get_user_birthdate(user_id)
    
    # Московское время для числа дня
    moscow_now = datetime.now(pytz.timezone('Europe/Moscow'))
    day_number = moscow_now.day + moscow_now.month
    while day_number > 9:
        day_number = sum(int(d) for d in str(day_number))
    
    day_descriptions = {
        1: "🔴 День лидерства! Бери инициативу в свои руки.",
        2: "🟠 День сотрудничества. Работа в команде принесёт успех.",
        3: "🟡 День творчества. Займись чем-то вдохновляющим.",
        4: "🟢 День порядка. Систематизируй дела.",
        5: "🔵 День свободы. Позволь себе новое.",
        6: "🔵 День заботы. Удели время близким.",
        7: "🟣 День мудрости. Прислушайся к интуиции.",
        8: "⚫️ День силы. Действуй решительно.",
        9: "⚪️ День завершения. Закрой старые долги."
    }
    
    if birth_date:
        day, month, _ = map(int, birth_date.split('.'))
        sign = get_zodiac_sign(day, month)
        forecasts = {
            "Овен": "🔥 Твоя энергия сегодня на пике!",
            "Телец": "💰 Удачный день для финансов.",
            "Близнецы": "💬 День общения и новостей.",
            "Рак": "🏠 Проведи время с семьёй.",
            "Лев": "🎭 Покажи свои таланты!",
            "Дева": "📋 Наведи порядок в делах.",
            "Весы": "⚖️ Ищи гармонию во всём.",
            "Скорпион": "🦂 Глубокие мысли приведут к ответам.",
            "Стрелец": "✈️ Открывай новые горизонты.",
            "Козерог": "🏔️ Достигай поставленных целей.",
            "Водолей": "💡 Гениальные идеи придут сами.",
            "Рыбы": "🎨 Погрузись в творчество."
        }
        horoscope = forecasts.get(sign, "🌟 Гармоничный день.")
    else:
        horoscope = "🌟 Гармоничный день."
        sign = "—"
    
    cards = ["Шут", "Маг", "Верховная Жрица", "Императрица", "Император", "Влюбленные", "Колесница", "Сила", "Отшельник", "Колесо Фортуны", "Справедливость", "Смерть", "Умеренность", "Звезда", "Луна", "Солнце", "Мир"]
    card_meanings = {
        "Шут": "🎭 Новое начало!", "Маг": "🪄 У тебя есть все ресурсы!", "Верховная Жрица": "🌙 Доверься интуиции.",
        "Императрица": "👑 Время творить!", "Император": "🏛️ Укрепляй границы.", "Влюбленные": "💕 Важный выбор.",
        "Колесница": "⚡ Управляй судьбой!", "Сила": "🦁 Ты сильнее, чем кажешься.", "Отшельник": "🏮 Время тишины.",
        "Колесо Фортуны": "🎡 Перемены к лучшему.", "Справедливость": "⚖️ Поступи справедливо.", "Смерть": "♻️ Старое уходит.",
        "Умеренность": "⚖️ Найди баланс.", "Звезда": "⭐ Верь в лучшее!", "Луна": "🌕 Доверяй интуиции.",
        "Солнце": "☀️ Всё будет хорошо!", "Мир": "🌍 Ты достигла цели!"
    }
    card = random.choice(cards)
    
    affirmations = [
        "✨ Я открыта новым возможностям. Вселенная заботится обо мне.",
        "✨ Мои таланты признаны и ценны.",
        "✨ Я привлекаю успех и изобилие.",
        "✨ Моя интуиция ведёт меня правильным путём.",
        "✨ Я люблю и принимаю себя целиком.",
        "✨ Каждый день я становлюсь сильнее."
    ]
    affirmation = random.choice(affirmations)
    
    text = f"""
🌅 **Доброе утро, {name}!** 🌅

🔮 **Число дня: {day_number}**
{day_descriptions.get(day_number, "Хороший день!")}

⭐ **Гороскоп для {sign}**
{horoscope}

🎴 **Карта дня: {card}**
{card_meanings.get(card, "Прислушайся к себе.")}

✨ **Аффирмация дня**
{affirmation}

━━━━━━━━━━━━━━━━━━━━━━━━━━
💎 У вас активна Premium-подписка!
📄 Полный PDF-отчёт доступен в меню
━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
    return text

# ========== ФОНОВАЯ ЗАДАЧА ДЛЯ ОТПРАВКИ В 8:00 ПО МОСКВЕ ==========
async def send_daily_premium_forecasts():
    """Каждый день в 8:00 по московскому времени отправляет прогнозы всем Premium-пользователям"""
    moscow_tz = pytz.timezone('Europe/Moscow')
    
    while True:
        now = datetime.now(moscow_tz)
        # Проверяем, что сейчас 8:00 утра по Москве
        if now.hour == 8 and now.minute == 0:
            users = get_all_premium_users()
            print(f"📬 Отправка утренних отчётов в {now.strftime('%H:%M')} MSK для {len(users)} пользователей")
            for user_id in users:
                if not was_forecast_sent_today(user_id):
                    try:
                        forecast = await generate_daily_forecast(user_id)
                        await bot.send_message(user_id, forecast, parse_mode="Markdown")
                        mark_forecast_sent(user_id)
                        print(f"📬 Утренний отчёт отправлен {user_id}")
                        await asyncio.sleep(1)
                    except Exception as e:
                        print(f"❌ Ошибка отправки {user_id}: {e}")
            # Ждём до следующего часа, чтобы не отправлять повторно
            await asyncio.sleep(60)
        await asyncio.sleep(30)

# ========== ИСТОРИЯ ДИАЛОГОВ ==========
user_history = {}
user_problems = {}

def get_system_prompt(gender: str, name: str) -> str:
    return f"""Ты — эмпатичный психолог-помощник по имени {PSYCHOLOGIST_NAME}. Пользователь — {'девушка' if gender == 'female' else 'парень'} по имени {name}. Отвечай на русском, коротко (2-4 предложения). После 4-6 обменов предложи записаться к психологу, добавив в конце "ЗАПИСЬ_ГОТОВА"."""

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

# ========== GOOGLE SHEETS ==========
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
            return False
        moscow_time = datetime.now(pytz.timezone('Europe/Moscow'))
        creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
        creds = Credentials.from_service_account_info(creds_dict, scopes=["https://www.googleapis.com/auth/spreadsheets"])
        client = gspread.authorize(creds)
        sheet = client.open_by_key(SHEET_ID).sheet1
        if not sheet.get_all_values():
            headers = ["Timestamp", "User ID", "Username", "Problem", "Direction", "Contact", "Status"]
            sheet.append_row(headers)
        row = [moscow_time.strftime("%Y-%m-%d %H:%M:%S"), user_id, username, problem[:200], direction, contact, "new"]
        sheet.append_row(row)
        return True
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        return False

# ========== ОБРАБОТЧИКИ ==========
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
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
        remaining = get_remaining_questions(user_id)
        status = "💎 Premium" if is_premium(user_id) else f"📊 {remaining}/{FREE_QUESTIONS_PER_DAY} вопросов"
        await message.answer(
            f"✨ **С возвращением!** ✨\n\n🌸 Я {PSYCHOLOGIST_NAME}.\nСтатус: {status}\n\n👇 Используй кнопки меню!",
            reply_markup=menu_keyboard,
            parse_mode="Markdown"
        )
        await state.set_state(Dialogue.chatting)
        return
    
    await state.set_state(Dialogue.choosing_gender)
    await message.answer(
        f"✨ **Привет, {message.from_user.first_name or 'друг'}!** ✨\n\n🌸 Я {PSYCHOLOGIST_NAME}.\n\n👇 Выбери свой пол:",
        reply_markup=gender_keyboard,
        parse_mode="Markdown"
    )

@dp.message(StateFilter(Dialogue.choosing_gender))
async def process_gender(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    text = message.text.lower()
    if "жен" in text:
        gender = "female"
        greeting = "👩 Рада знакомству, прекрасная дама!"
    elif "муж" in text:
        gender = "male"
        greeting = "👨 Рада знакомству, благородный рыцарь!"
    else:
        await message.answer("Выбери пол 👇", reply_markup=gender_keyboard)
        return
    
    set_user_gender(user_id, message.from_user.username or "", gender, message.from_user.first_name or "друг")
    await state.clear()
    remaining = get_remaining_questions(user_id)
    await message.answer(
        f"{greeting}\n\n🌸 Я {PSYCHOLOGIST_NAME}.\n📊 Лимит: {remaining}/{FREE_QUESTIONS_PER_DAY} вопросов сегодня.\n\n👇 Используй кнопки меню!",
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
    await message.answer("🧹 История очищена.", reply_markup=menu_keyboard)
    await state.set_state(Dialogue.chatting)

@dp.message(F.text == "ℹ️ Помощь")
async def menu_help(message: types.Message):
    remaining = get_remaining_questions(message.from_user.id)
    await message.answer(
        f"📖 **Что я умею?**\n\n📊 Осталось вопросов: {remaining}/{FREE_QUESTIONS_PER_DAY}\n\n"
        f"💬 Просто напиши\n🔮 Число судьбы\n⭐ Гороскоп\n♊ Совместимость\n🎴 Карта дня Таро\n"
        f"📞 Запись к психологу\n📊 Демо-отчёт\n📄 PDF-отчёт (Premium)\n⭐ Подписка Premium\n\n"
        f"🗑 /reset\n❌ /cancel",
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
    await message.answer("🔮 **Число судьбы**\n\nВведи дату рождения `ДД.ММ.ГГГГ`\nПример: 15.05.1990", parse_mode="Markdown")

@dp.message(StateFilter(Dialogue.waiting_for_birthdate))
async def process_fate_number(message: types.Message, state: FSMContext):
    if not re.match(r'^\d{2}\.\d{2}\.\d{4}$', message.text):
        await message.answer("❌ Неверный формат.", reply_markup=menu_keyboard)
        return
    user_id = message.from_user.id
    gender = get_user_gender(user_id)
    birth_date = message.text
    save_user_birthdate(user_id, birth_date)
    number, desc = calculate_fate_number(birth_date, gender)
    await message.answer(f"🔮 **Твоё число судьбы — {number}** 🔮\n\n{desc}", parse_mode="Markdown", reply_markup=menu_keyboard)
    await state.set_state(Dialogue.chatting)

@dp.message(F.text == "⭐ Гороскоп")
async def horoscope_start(message: types.Message, state: FSMContext):
    await state.set_state(Dialogue.waiting_for_zodiac)
    await message.answer("⭐ **Гороскоп**\n\nВведи знак или дату `ДД.ММ.ГГГГ`", parse_mode="Markdown")

@dp.message(StateFilter(Dialogue.waiting_for_zodiac))
async def process_horoscope(message: types.Message, state: FSMContext):
    text = message.text.strip()
    if re.match(r'^\d{2}\.\d{2}\.\d{4}$', text):
        day, month, _ = map(int, text.split('.'))
        sign = get_zodiac_sign(day, month)
    else:
        known = {"овен":"Овен","телец":"Телец","близнецы":"Близнецы","рак":"Рак","лев":"Лев","дева":"Дева","весы":"Весы","скорпион":"Скорпион","стрелец":"Стрелец","козерог":"Козерог","водолей":"Водолей","рыбы":"Рыбы"}
        sign = known.get(text.lower())
        if not sign:
            await message.answer("❌ Неизвестный знак.", reply_markup=menu_keyboard)
            return
    forecasts = {
        "Овен": "🔥 Энергия бьёт ключом!", "Телец": "💰 Хороший день для финансов.",
        "Близнецы": "💬 День общения.", "Рак": "🏠 День семьи.", "Лев": "🎭 Творческий день.",
        "Дева": "📋 День порядка.", "Весы": "⚖️ День гармонии.", "Скорпион": "🦂 День трансформации.",
        "Стрелец": "✈️ День приключений.", "Козерог": "🏔️ День достижений.",
        "Водолей": "💡 День идей.", "Рыбы": "🎨 День творчества."
    }
    await message.answer(f"✨ **Гороскоп для {sign}** ✨\n\n{forecasts.get(sign, 'Гармоничный день.')}", parse_mode="Markdown", reply_markup=menu_keyboard)
    await state.set_state(Dialogue.chatting)

@dp.message(F.text == "♊ Совместимость")
async def compatibility_start(message: types.Message, state: FSMContext):
    await state.set_state(Dialogue.waiting_for_birthdate_comp)
    await message.answer("💕 **Совместимость**\n\nВведи первую дату `ДД.ММ.ГГГГ`", parse_mode="Markdown")

@dp.message(StateFilter(Dialogue.waiting_for_birthdate_comp))
async def process_compatibility_first(message: types.Message, state: FSMContext):
    if not re.match(r'^\d{2}\.\d{2}\.\d{4}$', message.text):
        await message.answer("❌ Неверный формат.", reply_markup=menu_keyboard)
        return
    await state.update_data(date1=message.text)
    await state.set_state(Dialogue.waiting_for_birthdate_comp2)
    await message.answer("Введи вторую дату `ДД.ММ.ГГГГ`", parse_mode="Markdown")

@dp.message(StateFilter(Dialogue.waiting_for_birthdate_comp2))
async def process_compatibility_second(message: types.Message, state: FSMContext):
    if not re.match(r'^\d{2}\.\d{2}\.\d{4}$', message.text):
        await message.answer("❌ Неверный формат.", reply_markup=menu_keyboard)
        return
    data = await state.get_data()
    date1 = data.get('date1')
    if not date1:
        await message.answer("❌ Ошибка.", reply_markup=menu_keyboard)
        return
    premium = is_premium(message.from_user.id)
    result = get_compatibility(date1, message.text, premium)
    await message.answer(f"💕 **Результат**\n\n📅 {date1} → {result['sign1']}\n📅 {message.text} → {result['sign2']}\n\n🌟 {result['percent']}%\n{result['text']}", parse_mode="Markdown", reply_markup=menu_keyboard)
    await state.set_state(Dialogue.chatting)

@dp.message(F.text == "🎴 Карта дня Таро")
async def taro_card_handler(message: types.Message):
    cards = ["Шут", "Маг", "Верховная Жрица", "Императрица", "Император", "Влюбленные", "Колесница", "Сила", "Отшельник", "Колесо Фортуны", "Справедливость", "Смерть", "Умеренность", "Звезда", "Луна", "Солнце", "Мир"]
    meanings = {
        "Шут": "🎭 Новое начало!", "Маг": "🪄 У тебя есть все ресурсы!", "Верховная Жрица": "🌙 Доверься интуиции.",
        "Императрица": "👑 Время творить!", "Император": "🏛️ Укрепляй границы.", "Влюбленные": "💕 Важный выбор.",
        "Колесница": "⚡ Управляй судьбой!", "Сила": "🦁 Ты сильнее, чем кажешься.", "Отшельник": "🏮 Время тишины.",
        "Колесо Фортуны": "🎡 Перемены к лучшему.", "Справедливость": "⚖️ Поступи справедливо.", "Смерть": "♻️ Старое уходит.",
        "Умеренность": "⚖️ Найди баланс.", "Звезда": "⭐ Верь в лучшее!", "Луна": "🌕 Доверяй интуиции.",
        "Солнце": "☀️ Всё будет хорошо!", "Мир": "🌍 Ты достигла цели!"
    }
    card = random.choice(cards)
    await message.answer(f"🎴 **Карта дня: {card}**\n\n{meanings[card]}", parse_mode="Markdown", reply_markup=menu_keyboard)

@dp.message(F.text == "📞 Запись к психологу")
async def book_psychologist(message: types.Message, state: FSMContext):
    await message.answer("🌸 **Запись**\n\nОставь контакт (@username или телефон)", reply_markup=ReplyKeyboardRemove())
    await state.set_state(Dialogue.waiting_for_contact)

@dp.message(StateFilter(Dialogue.waiting_for_contact))
async def process_contact(message: types.Message, state: FSMContext):
    contact = message.text
    user_id = message.from_user.id
    username = message.from_user.username or "None"
    problem_info = user_problems.get(user_id, {"problem": "Диалог", "direction": "общее"})
    save_to_google_sheets(user_id, username, problem_info["problem"], problem_info["direction"], contact)
    await notify_psychologist(user_id, username, problem_info["problem"], problem_info["direction"], contact)
    if user_id in user_history:
        del user_history[user_id]
    if user_id in user_problems:
        del user_problems[user_id]
    await message.answer(f"🌸 Спасибо! Психолог свяжется с тобой.", reply_markup=menu_keyboard)
    await state.clear()
    await state.set_state(Dialogue.chatting)

@dp.message(F.text == "⭐ Подписка Premium")
async def show_premium_info(message: types.Message):
    if is_premium(message.from_user.id):
        await message.answer("💎 Premium активна! Спасибо за поддержку!\n\n📬 Каждое утро в 8:00 вы получаете персональный прогноз!", reply_markup=menu_keyboard)
    else:
        remaining = get_remaining_questions(message.from_user.id)
        await message.answer(
            f"⭐ **Premium 99 Stars/мес** ⭐\n\n📊 Лимит: {remaining}/{FREE_QUESTIONS_PER_DAY}\n\n"
            f"💎 **Что даёт:**\n✅ Безлимитные вопросы\n✅ Расширенная совместимость\n✅ PDF-отчёт\n✅ Ежедневный прогноз в 8:00\n✅ Приоритетная поддержка",
            reply_markup=premium_keyboard
        )

@dp.callback_query(lambda c: c.data == "what_is_premium")
async def what_is_premium(callback: types.CallbackQuery):
    await callback.answer()
    await callback.message.answer(
        "🔮 **Premium 99 Stars** 🔮\n\n"
        "1️⃣ Безлимитные вопросы\n2️⃣ Полный разбор совместимости\n3️⃣ PDF-отчёт\n4️⃣ Ежедневный прогноз в 8:00\n5️⃣ Приоритетная поддержка\n\n💎 Нажми «Оформить подписку»!"
    )

@dp.callback_query(lambda c: c.data == "buy_subscription")
async def buy_subscription(callback: types.CallbackQuery):
    await callback.answer()
    prices = [LabeledPrice(label="Premium", amount=SUBSCRIPTION_PRICE)]
    await callback.message.answer_invoice(
        title="Premium-подписка",
        description="Безлимитные вопросы + PDF-отчёт + ежедневные прогнозы",
        payload="premium_30d",
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
    activate_premium(message.from_user.id, 30)
    await message.answer(
        "💎 **Premium активирована!** 💎\n\n"
        "✨ Теперь вам доступны:\n"
        "✅ Безлимитные вопросы\n"
        "✅ PDF-отчёт (кнопка в меню)\n"
        "✅ Ежедневный прогноз в 8:00\n\n"
        "Спасибо за поддержку!",
        reply_markup=menu_keyboard
    )

@dp.message(F.text == "📊 Демо-отчёт")
async def show_demo_report(message: types.Message):
    birth_date = get_user_birthdate(message.from_user.id)
    if not birth_date:
        await message.answer("📊 Сначала укажи дату рождения через «Число судьбы».", reply_markup=menu_keyboard)
        return
    await message.answer(
        "📄 **Демо-отчёт**\n\n🔮 Число судьбы: 7\n⭐ Гороскоп: гармоничный день\n🎴 Карта: Звезда\n\n"
        "✨ В полном отчёте (Premium): 15+ страниц, совместимость, PDF-файл.\n\n💎 Всего за 99 Stars/мес!\n"
        "📬 Также Premium-пользователи получают ежедневный прогноз в 8:00!",
        reply_markup=menu_keyboard
    )

@dp.message(F.text == "📄 Получить PDF-отчёт")
async def get_pdf_report(message: types.Message):
    user_id = message.from_user.id
    if not is_premium(user_id):
        await message.answer("💎 PDF-отчёт только для Premium! Оформи подписку.", reply_markup=menu_keyboard)
        return
    if not get_user_birthdate(user_id):
        await message.answer("🔮 Сначала укажи дату рождения через «Число судьбы».", reply_markup=menu_keyboard)
        return
    await message.answer("💕 Добавить совместимость с партнёром?", reply_markup=partner_keyboard)

@dp.callback_query(lambda c: c.data == "pdf_without_partner")
async def pdf_without_partner(callback: types.CallbackQuery):
    await callback.answer()
    await callback.message.answer("📄 Генерирую отчёт...")
    pdf = await generate_pdf_report(callback.from_user.id, None)
    await callback.message.answer_document(
        document=BufferedInputFile(pdf.getvalue(), filename=f"otchet_{callback.from_user.id}.pdf"),
        caption="✨ Твой отчёт готов! ✨",
        reply_markup=menu_keyboard
    )

@dp.callback_query(lambda c: c.data == "pdf_with_partner")
async def pdf_with_partner(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.answer("💕 Введи дату рождения партнёра в формате `ДД.ММ.ГГГГ`", parse_mode="Markdown")
    await state.set_state(Dialogue.waiting_for_partner_date)

@dp.message(StateFilter(Dialogue.waiting_for_partner_date))
async def process_partner_date(message: types.Message, state: FSMContext):
    if not re.match(r'^\d{2}\.\d{2}\.\d{4}$', message.text):
        await message.answer("❌ Неверный формат.", reply_markup=menu_keyboard)
        return
    partner_date = message.text
    await state.clear()
    await message.answer("📄 Генерирую отчёт...")
    pdf = await generate_pdf_report(message.from_user.id, partner_date)
    await message.answer_document(
        document=BufferedInputFile(pdf.getvalue(), filename=f"otchet_{message.from_user.id}.pdf"),
        caption=f"✨ Отчёт с совместимостью готов! Дата партнёра: {partner_date}",
        reply_markup=menu_keyboard
    )

# ========== ОСНОВНОЙ ДИАЛОГ ==========
@dp.message(Dialogue.chatting)
async def chat_with_ai(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    user_text = message.text
    
    menu_buttons = ["ℹ️ Помощь", "🗑 Очистить диалог", "🔮 Число судьбы", "⭐ Гороскоп", "♊ Совместимость", "🎴 Карта дня Таро", "📞 Запись к психологу", "⭐ Подписка Premium", "📊 Демо-отчёт", "📄 Получить PDF-отчёт"]
    if user_text in menu_buttons:
        return
    
    remaining = get_remaining_questions(user_id)
    if remaining <= 0 and not is_premium(user_id):
        await message.answer(f"📊 Лимит вопросов исчерпан ({FREE_QUESTIONS_PER_DAY}).\n\n⭐ Оформи Premium за 99 Stars!", reply_markup=menu_keyboard)
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
        
        if "ЗАПИСЬ_ГОТОВА" in answer:
            answer = answer.replace("ЗАПИСЬ_ГОТОВА", "").strip()
            if answer:
                await message.answer(answer)
            book_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📝 Записаться", callback_data="book")], [InlineKeyboardButton(text="❌ Не сейчас", callback_data="not_ready")]])
            await message.answer("💕 Хочешь обсудить это с психологом?", reply_markup=book_kb)
        else:
            await message.answer(answer)
            
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        await message.answer("🌙 Ошибка. Попробуй ещё раз.")

@dp.callback_query(lambda c: c.data == "book")
async def handle_book(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.answer("🌸 Оставь контакт (@username или телефон)", reply_markup=ReplyKeyboardRemove())
    await state.set_state(Dialogue.waiting_for_contact)

@dp.callback_query(lambda c: c.data == "not_ready")
async def handle_not_ready(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await callback.message.answer("🌿 Хорошо. Напиши /start когда будешь готова.", reply_markup=menu_keyboard)
    await state.set_state(Dialogue.chatting)

# ========== ЗАПУСК ==========
async def main():
    print("✨ Бот с ежедневными уведомлениями для Premium в 8:00 по Москве запущен! ✨")
    asyncio.create_task(send_daily_premium_forecasts())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
