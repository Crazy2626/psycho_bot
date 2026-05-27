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
    waiting_for_partner_date = State()  # для PDF

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
    subheading_style = ParagraphStyle('Subheading', parent=styles['Heading2'], fontSize=14, textColor='#6A1B9A', spaceAfter=8)
    normal_style = ParagraphStyle('Normal', parent=styles['Normal'], fontSize=11, leading=14, spaceAfter=6)
    
    # Титульная страница
    story.append(Paragraph(f"<b>Персональный отчёт</b>", title_style))
    story.append(Spacer(1, 0.5*cm))
    story.append(Paragraph(f"<font size=16>для {name}</font>", title_style))
    story.append(Spacer(1, 1*cm))
    story.append(Paragraph(f"📅 Дата рождения: {birth_date}", normal_style))
    story.append(Paragraph(f"✨ Пол: {'Женский' if gender == 'female' else 'Мужской'}", normal_style))
    story.append(Paragraph(f"📆 Дата составления: {datetime.now().strftime('%d.%m.%Y')}", normal_style))
    story.append(PageBreak())
    
    # 1. Число судьбы
    fate_number, fate_desc = calculate_fate_number(birth_date, gender)
    story.append(Paragraph(f"🔮 Число судьбы — {fate_number}", heading_style))
    story.append(Paragraph(fate_desc, normal_style))
    story.append(Spacer(1, 0.5*cm))
    
    # 2. Гороскоп на месяц
    day, month, _ = map(int, birth_date.split('.'))
    sign = get_zodiac_sign(day, month)
    story.append(Paragraph(f"⭐ Гороскоп для {sign} на {datetime.now().strftime('%B %Y')}", heading_style))
    forecasts = {
        "Овен": "Ваша энергия на пике! Используйте её для новых начинаний. Середина месяца принесёт приятные сюрпризы.",
        "Телец": "Финансовый месяц. Возможны крупные покупки. В конце месяца — удачные переговоры.",
        "Близнецы": "Месяц общения. Старые друзья напомнят о себе. Возможна короткая поездка.",
        "Рак": "Время семьи и дома. Укрепляйте отношения с близкими.",
        "Лев": "Творческий подъём. Ваши таланты будут замечены.",
        "Дева": "Месяц порядка. Завершите старые дела.",
        "Весы": "Гармония во всём. Хорошее время для новых знакомств.",
        "Скорпион": "Глубокий самоанализ. Ответы придут изнутри.",
        "Стрелец": "Время путешествий. Возможно обучение.",
        "Козерог": "Карьерный рост. Ваши усилия оценят.",
        "Водолей": "Время идей. Записывайте всё, что приходит в голову.",
        "Рыбы": "Творчество и интуиция. Займитесь искусством."
    }
    story.append(Paragraph(forecasts.get(sign, "Гармоничный месяц. Доверяйте себе."), normal_style))
    story.append(Spacer(1, 0.5*cm))
    
    # 3. Совместимость (если запрошена)
    if partner_date:
        comp = get_compatibility(birth_date, partner_date, premium=True)
        story.append(Paragraph(f"💕 Совместимость с партнёром", heading_style))
        story.append(Paragraph(f"📅 Ваша дата: {birth_date} → {comp['sign1']}", normal_style))
        story.append(Paragraph(f"📅 Дата партнёра: {partner_date} → {comp['sign2']}", normal_style))
        story.append(Spacer(1, 0.3*cm))
        story.append(Paragraph(f"🌟 Совместимость: {comp['percent']}%", subheading_style))
        story.append(Paragraph(comp['text'], normal_style))
        story.append(Spacer(1, 0.5*cm))
    
    # 4. Расклад Таро
    story.append(Paragraph(f"🎴 Расклад «Путь года»", heading_style))
    taro_spreads = [
        ("1. Вы сейчас", "Маг 🪄 — «У вас есть всё для нового этапа»"),
        ("2. Что вас ждёт", "Колесница ⚡ — «Время действовать и побеждать»"),
        ("3. Испытания", "Звезда ⭐ — «Надежда поможет преодолеть трудности»"),
        ("4. Помощь", "Сила 🦁 — «Внутренняя мощь поведёт вас»"),
        ("5. Любовь", "Влюблённые 💕 — «Судьбоносная встреча или важный выбор»"),
        ("6. Карьера", "Император 🏛️ — «Укрепление позиций или повышение»"),
        ("7. Финансы", "Десятка Пентаклей 💰 — «Стабильный доход»"),
        ("8. Здоровье", "Умеренность ⚖️ — «Баланс между работой и отдыхом»"),
        ("9. Духовный рост", "Отшельник 🏮 — «Год глубокого самоанализа»"),
        ("10. Итог года", "Мир 🌍 — «Завершение цикла, достижение цели»")
    ]
    for card, meaning in taro_spreads:
        story.append(Paragraph(f"<b>{card}:</b> {meaning}", normal_style))
        story.append(Spacer(1, 0.2*cm))
    
    story.append(Spacer(1, 0.5*cm))
    
    # 5. Аффирмации
    story.append(Paragraph(f"✨ Аффирмации на {datetime.now().strftime('%B')}", heading_style))
    affirmations = [
        f"1 {datetime.now().strftime('%d.%m')}: «Я открыта новым возможностям. Вселенная заботится обо мне»",
        "2: «Мои таланты признаны и ценны»",
        "3: «Я привлекаю успех и изобилие»",
        "4: «Моя интуиция ведёт меня правильным путём»",
        "5: «Я люблю и принимаю себя целиком»",
        "6: «Каждый день я становлюсь сильнее»"
    ]
    for aff in affirmations:
        story.append(Paragraph(aff, normal_style))
        story.append(Spacer(1, 0.2*cm))
    
    story.append(Spacer(1, 0.5*cm))
    
    # 6. Заключение
    story.append(Paragraph(f"💫 Персональные рекомендации", heading_style))
    story.append(Paragraph("✨ Доверяйте своей интуиции — она редко ошибается.", normal_style))
    story.append(Paragraph("✨ Уделяйте время отдыху и восстановлению.", normal_style))
    story.append(Paragraph("✨ Не бойтесь просить о помощи, когда она нужна.", normal_style))
    story.append(Paragraph("✨ Благодарите себя и других каждый день.", normal_style))
    story.append(Spacer(1, 0.5*cm))
    story.append(Paragraph("🌿 Благодарим за доверие! Берегите себя и будьте счастливы 💕", normal_style))
    
    doc.build(story)
    buffer.seek(0)
    return buffer

# ========== ИСТОРИЯ ДИАЛОГОВ ==========
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

Отвечай коротко (2-4 предложения) на русском."""

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
        return True
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        return False

# ========== ОБРАБОТЧИКИ КОМАНД ==========
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
            f"✨ **С {greeting}, {name}!** ✨\n\n🌸 Я {PSYCHOLOGIST_NAME}, твой помощник.\nТвой статус: {premium_status}\n\n💫 Используй кнопки меню!",
            reply_markup=menu_keyboard,
            parse_mode="Markdown"
        )
        await state.set_state(Dialogue.chatting)
        return
    
    await state.set_state(Dialogue.choosing_gender)
    await message.answer(
        f"✨ **Привет, {message.from_user.first_name or 'друг'}!** ✨\n\n🌸 Я {PSYCHOLOGIST_NAME}.\n\n💫 Выбери свой пол:",
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
        await message.answer("Выбери свой пол 👇", reply_markup=gender_keyboard)
        return
    
    set_user_gender(user_id, message.from_user.username or "", gender, message.from_user.first_name or "друг")
    remaining = get_remaining_questions(user_id)
    
    await state.clear()
    await message.answer(
        f"{greeting}\n\n🌸 Я {PSYCHOLOGIST_NAME}, твой помощник.\n📊 Лимит сегодня: {remaining}/{FREE_QUESTIONS_PER_DAY} вопросов.\n⭐ Подписка Premium снимает лимиты.\n\n👇 Используй кнопки меню!",
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
    user_id = message.from_user.id
    remaining = get_remaining_questions(user_id)
    await message.answer(
        f"📖 **Что я умею?**\n\n📊 Осталось вопросов: {remaining}/{FREE_QUESTIONS_PER_DAY}\n\n"
        f"💬 **Просто напиши** — поддержка\n🔮 **Число судьбы** — по дате\n⭐ **Гороскоп**\n♊ **Совместимость**\n🎴 **Карта дня Таро**\n"
        f"📞 **Запись к психологу**\n📊 **Демо-отчёт**\n📄 **PDF-отчёт** (Premium)\n⭐ **Подписка Premium**\n\n🗑 /reset\n❌ /cancel",
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
    await message.answer("🔮 **Расчёт числа судьбы**\n\nВведи дату рождения `ДД.ММ.ГГГГ`\n🌙 Пример: 15.05.1990", parse_mode="Markdown")

@dp.message(StateFilter(Dialogue.waiting_for_birthdate))
async def process_fate_number(message: types.Message, state: FSMContext):
    if not re.match(r'^\d{2}\.\d{2}\.\d{4}$', message.text):
        await message.answer("❌ Неверный формат. Введи как `ДД.ММ.ГГГГ`", reply_markup=menu_keyboard)
        return
    user_id = message.from_user.id
    gender = get_user_gender(user_id)
    birth_date = message.text
    save_user_birthdate(user_id, birth_date)
    number, description = calculate_fate_number(birth_date, gender)
    await message.answer(f"🔮 **Твоё число судьбы — {number}** 🔮\n\n{description}\n\n📊 Теперь доступен демо-отчёт!", parse_mode="Markdown", reply_markup=menu_keyboard)
    await state.set_state(Dialogue.chatting)

@dp.message(F.text == "⭐ Гороскоп")
async def horoscope_start(message: types.Message, state: FSMContext):
    await state.set_state(Dialogue.waiting_for_zodiac)
    await message.answer("⭐ **Гороскоп**\n\nВведи знак зодиака или дату `ДД.ММ.ГГГГ`", parse_mode="Markdown")

@dp.message(StateFilter(Dialogue.waiting_for_zodiac))
async def process_horoscope(message: types.Message, state: FSMContext):
    text = message.text.strip()
    zodiac_sign = None
    if re.match(r'^\d{2}\.\d{2}\.\d{4}$', text):
        day, month, _ = map(int, text.split('.'))
        zodiac_sign = get_zodiac_sign(day, month)
    else:
        known = {"овен":"Овен","телец":"Телец","близнецы":"Близнецы","рак":"Рак","лев":"Лев","дева":"Дева","весы":"Весы","скорпион":"Скорпион","стрелец":"Стрелец","козерог":"Козерог","водолей":"Водолей","рыбы":"Рыбы"}
        if text.lower() in known:
            zodiac_sign = known[text.lower()]
        else:
            await message.answer("❌ Неизвестный знак.", reply_markup=menu_keyboard)
            return
    forecasts = {
        "Овен": "🔥 Энергия бьёт ключом!",
        "Телец": "💰 Хороший день для финансов.",
        "Близнецы": "💬 День общения.",
        "Рак": "🏠 День семьи.",
        "Лев": "🎭 Творческий день.",
        "Дева": "📋 День порядка.",
        "Весы": "⚖️ День гармонии.",
        "Скорпион": "🦂 День трансформации.",
        "Стрелец": "✈️ День приключений.",
        "Козерог": "🏔️ День достижений.",
        "Водолей": "💡 День идей.",
        "Рыбы": "🎨 День творчества."
    }
    await message.answer(f"✨ **Гороскоп для {zodiac_sign}** ✨\n\n📅 {forecasts.get(zodiac_sign, 'Гармоничный день.')}", parse_mode="Markdown", reply_markup=menu_keyboard)
    await state.set_state(Dialogue.chatting)

@dp.message(F.text == "♊ Совместимость")
async def compatibility_start(message: types.Message, state: FSMContext):
    await state.set_state(Dialogue.waiting_for_birthdate_comp)
    await message.answer("💕 **Расчёт совместимости**\n\nВведи **первую** дату рождения `ДД.ММ.ГГГГ`", parse_mode="Markdown")

@dp.message(StateFilter(Dialogue.waiting_for_birthdate_comp))
async def process_compatibility_first(message: types.Message, state: FSMContext):
    if not re.match(r'^\d{2}\.\d{2}\.\d{4}$', message.text):
        await message.answer("❌ Неверный формат.", reply_markup=menu_keyboard)
        return
    await state.update_data(date1=message.text)
    await state.set_state(Dialogue.waiting_for_birthdate_comp2)
    await message.answer("💕 Теперь введи **вторую** дату рождения", parse_mode="Markdown")

@dp.message(StateFilter(Dialogue.waiting_for_birthdate_comp2))
async def process_compatibility_second(message: types.Message, state: FSMContext):
    if not re.match(r'^\d{2}\.\d{2}\.\d{4}$', message.text):
        await message.answer("❌ Неверный формат.", reply_markup=menu_keyboard)
        return
    data = await state.get_data()
    date1 = data.get('date1')
    if not date1:
        await message.answer("❌ Ошибка.", reply_markup=menu_keyboard)
        await state.clear()
        return
    user_id = message.from_user.id
    premium = is_premium(user_id)
    result = get_compatibility(date1, message.text, premium)
    await message.answer(f"💕 **Результат** 💕\n\n📅 {date1} → {result['sign1']}\n📅 {message.text} → {result['sign2']}\n\n🌟 {result['percent']}%\n{result['text']}", parse_mode="Markdown", reply_markup=menu_keyboard)
    await state.set_state(Dialogue.chatting)

@dp.message(F.text == "🎴 Карта дня Таро")
async def taro_card_handler(message: types.Message):
    cards = ["Шут", "Маг", "Верховная Жрица", "Императрица", "Император", "Иерофант", "Влюбленные", "Колесница", "Сила", "Отшельник", "Колесо Фортуны", "Справедливость", "Повешенный", "Смерть", "Умеренность", "Дьявол", "Башня", "Звезда", "Луна", "Солнце", "Суд", "Мир"]
    meanings = {
        "Шут": "🎭 Новое начало! Пора сделать первый шаг.",
        "Маг": "🪄 У тебя есть все ресурсы!",
        "Верховная Жрица": "🌙 Доверься интуиции.",
        "Императрица": "👑 Время творить.",
        "Император": "🏛️ Укрепляй границы.",
        "Иерофант": "⛪ Обратись к старшим.",
        "Влюбленные": "💕 Важный выбор.",
        "Колесница": "⚡ Управляй судьбой!",
        "Сила": "🦁 Ты сильнее, чем кажешься.",
        "Отшельник": "🏮 Время тишины.",
        "Колесо Фортуны": "🎡 Перемены к лучшему.",
        "Справедливость": "⚖️ Поступи справедливо.",
        "Повешенный": "🪢 Новый взгляд.",
        "Смерть": "♻️ Старое уходит.",
        "Умеренность": "⚖️ Найди баланс.",
        "Дьявол": "😈 Освободись.",
        "Башня": "🏛️💥 Крах иллюзий.",
        "Звезда": "⭐ Верь в лучшее!",
        "Луна": "🌕 Доверяй интуиции.",
        "Солнце": "☀️ Всё будет хорошо!",
        "Суд": "🎺 Время подвести итоги.",
        "Мир": "🌍 Ты достигла цели!"
    }
    card = random.choice(cards)
    await message.answer(f"🎴 **Карта дня: {card}** 🎴\n\n{meanings[card]}\n\n✨ Пусть день принесёт волшебство!", parse_mode="Markdown", reply_markup=menu_keyboard)

@dp.message(F.text == "📞 Запись к психологу")
async def book_psychologist(message: types.Message, state: FSMContext):
    await message.answer(
        "🌸 **Запись на консультацию** 🌸\n\nОставь контакт (@username или телефон), и психолог Дарья свяжется с тобой.\n\nИли /cancel",
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
    await message.answer(f"🌸 **Спасибо!** Психолог свяжется с тобой.\n\nБереги себя 💕", reply_markup=menu_keyboard)
    await state.clear()
    await state.set_state(Dialogue.chatting)

# ========== ПОДПИСКА PREMIUM ==========
@dp.message(F.text == "⭐ Подписка Premium")
async def show_premium_info(message: types.Message):
    user_id = message.from_user.id
    if is_premium(user_id):
        await message.answer("💎 Premium активна! Спасибо за поддержку! ✨", reply_markup=menu_keyboard)
    else:
        remaining = get_remaining_questions(user_id)
        await message.answer(
            f"⭐ **Premium-подписка 99 Stars/мес** ⭐\n\n📊 Лимит сегодня: {remaining}/{FREE_QUESTIONS_PER_DAY}\n\n"
            f"💎 **Что даёт:**\n✅ Безлимитные вопросы\n✅ Расширенная совместимость\n✅ Полный PDF-отчёт\n✅ Приоритетная поддержка\n\n"
            f"✨ Нажми кнопку ниже!",
            reply_markup=premium_keyboard
        )

@dp.callback_query(lambda c: c.data == "what_is_premium")
async def what_is_premium(callback: types.CallbackQuery):
    await callback.answer()
    await callback.message.answer(
        "🔮 **Premium-подписка 99 Stars/мес** 🔮\n\n"
        "1️⃣ Безлимитные консультации\n"
        "2️⃣ Полный разбор совместимости\n"
        "3️⃣ Расширенные прогнозы\n"
        "4️⃣ PDF-отчёт на 15+ страниц\n"
        "5️⃣ Приоритетная поддержка\n\n"
        "💎 Нажми «Оформить подписку»!"
    )

@dp.callback_query(lambda c: c.data == "buy_subscription")
async def buy_subscription(callback: types.CallbackQuery):
    await callback.answer()
    prices = [LabeledPrice(label="Premium-подписка на месяц", amount=SUBSCRIPTION_PRICE)]
    await callback.message.answer_invoice(
        title="Premium-подписка",
        description="Безлимитные консультации + расширенные функции + PDF-отчёт",
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
        "💎 **Premium активирована!** 💎\n\n"
        "✨ Доступны безлимитные консультации и PDF-отчёты!\n\n"
        "Спасибо за поддержку! 🙏",
        reply_markup=menu_keyboard
    )

# ========== ДЕМО-ОТЧЁТ ==========
@dp.message(F.text == "📊 Демо-отчёт")
async def show_demo_report(message: types.Message):
    user_id = message.from_user.id
    birth_date = get_user_birthdate(user_id)
    if not birth_date:
        await message.answer(
            "📊 **Демо-отчёт**\n\nСначала укажи дату рождения через кнопку «Число судьбы».",
            reply_markup=menu_keyboard
        )
        return
    
    gender = get_user_gender(user_id)
    name = get_user_name(user_id)
    number, desc = calculate_fate_number(birth_date, gender)
    day, month, _ = map(int, birth_date.split('.'))
    sign = get_zodiac_sign(day, month)
    
    demo_text = f"""
📄 **ПЕРСОНАЛЬНЫЙ ДЕМО-ОТЧЁТ**
━━━━━━━━━━━━━━━━━━━━━━━━━━

🔮 **Число судьбы — {number}**
{desc[:200]}...

━━━━━━━━━━━━━━━━━━━━━━━━━━

⭐ **Гороскоп для {sign}**

Общие тенденции этого года благоприятны для новых начинаний.

━━━━━━━━━━━━━━━━━━━━━━━━━━

💕 **Совместимость (пример)**
С Весами → 86%
Сильные стороны: взаимное вдохновение

━━━━━━━━━━━━━━━━━━━━━━━━━━

✨ **В полном отчёте (Premium):**
• 15+ страниц персонализированного анализа
• Детальный разбор совместимости
• Расклад Таро «Путь года» (10 карт)
• Ежедневные аффирмации на месяц
• Лунный календарь
• PDF-файл для скачивания

💎 **Всего за 99 Stars/мес!**
"""
    await message.answer(demo_text, parse_mode="Markdown", reply_markup=menu_keyboard)
    await message.answer("👉 Нажми «⭐ Подписка Premium», чтобы получить полную версию!", reply_markup=premium_keyboard)

# ========== PDF-ОТЧЁТ (PREMIUM) ==========
@dp.message(F.text == "📄 Получить PDF-отчёт")
async def get_pdf_report(message: types.Message):
    user_id = message.from_user.id
    
    if not is_premium(user_id):
        await message.answer(
            "💎 **PDF-отчёт доступен только Premium-пользователям!**\n\n"
            "Оформи подписку за 99 Stars/мес.\n\n👉 Нажми «⭐ Подписка Premium» в меню.",
            reply_markup=menu_keyboard
        )
        return
    
    birth_date = get_user_birthdate(user_id)
    if not birth_date:
        await message.answer("🔮 Сначала укажи дату рождения через кнопку «Число судьбы».", reply_markup=menu_keyboard)
        return
    
    await message.answer(
        "📄 **Генерация PDF-отчёта...**\n\nПожалуйста, подождите 10-20 секунд ✨",
        reply_markup=menu_keyboard
    )
    
    await message.answer(
        "💕 **Хотите добавить анализ совместимости с партнёром?**\n\nЭто увеличит отчёт на 2-3 страницы.",
        reply_markup=partner_keyboard
    )

@dp.callback_query(lambda c: c.data == "pdf_without_partner")
async def pdf_without_partner(callback: types.CallbackQuery):
    await callback.answer()
    await callback.message.answer("📄 Генерирую отчёт...")
    pdf_buffer = await generate_pdf_report(callback.from_user.id, None)
    await callback.message.answer_document(
        document=BufferedInputFile(pdf_buffer.getvalue(), filename=f"otchet_{callback.from_user.id}.pdf"),
        caption="✨ **Ваш персональный отчёт готов!** ✨\n\nБлагодарим за доверие! 💕",
        reply_markup=menu_keyboard
    )

@dp.callback_query(lambda c: c.data == "pdf_with_partner")
async def pdf_with_partner(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.answer(
        "💕 **Введите дату рождения партнёра**\n\nВ формате `ДД.ММ.ГГГГ`, например: 15.05.1990",
        parse_mode="Markdown"
    )
    await state.set_state(Dialogue.waiting_for_partner_date)

@dp.message(StateFilter(Dialogue.waiting_for_partner_date))
async def process_partner_date(message: types.Message, state: FSMContext):
    if not re.match(r'^\d{2}\.\d{2}\.\d{4}$', message.text):
        await message.answer("❌ Неверный формат. Введите как `ДД.ММ.ГГГГ`", parse_mode="Markdown")
        return
    
    partner_date = message.text
    user_id = message.from_user.id
    await state.clear()
    
    await message.answer("📄 Генерирую отчёт с совместимостью...")
    pdf_buffer = await generate_pdf_report(user_id, partner_date)
    await message.answer_document(
        document=BufferedInputFile(pdf_buffer.getvalue(), filename=f"otchet_{user_id}.pdf"),
        caption=f"✨ **Ваш отчёт с анализом совместимости готов!** ✨\n\n📅 Дата партнёра: {partner_date}\n\nБлагодарим за доверие! 💕",
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
    
    print(f"📨 Получено: {user_text}")
    
    crisis = ["суицид", "самоубийств", "не хочу жить", "покончить с собой", "умру"]
    if any(word in user_text.lower() for word in crisis):
        await message.answer("🕊️ Телефон доверия: 8-800-2000-122. Пожалуйста, позвони ❤️")
        return
    
    remaining = get_remaining_questions(user_id)
    if remaining <= 0 and not is_premium(user_id):
        await message.answer(
            f"📊 **Лимит вопросов на сегодня исчерпан** ({FREE_QUESTIONS_PER_DAY}).\n\n"
            f"⭐ Оформи Premium за 99 Stars/мес для безлимита!\n\n👉 Нажми «⭐ Подписка Premium».",
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
            book_kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📝 Да, записаться!", callback_data="book")],
                [InlineKeyboardButton(text="❌ Пока не готов", callback_data="not_ready")]
            ])
            await message.answer(
                f"💕 **Хочешь обсудить это с психологом {PSYCHOLOGIST_NAME}?**\n\nЭто конфиденциально.",
                reply_markup=book_kb
            )
        else:
            if not is_premium(user_id):
                answer += f"\n\n📊 Осталось вопросов: {new_remaining}/{FREE_QUESTIONS_PER_DAY}"
            await message.answer(answer)
            
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        await message.answer("🌙 Ошибка. Попробуй ещё раз или используй кнопки меню.", reply_markup=menu_keyboard)

@dp.callback_query(lambda c: c.data == "book")
async def handle_book(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.answer(
        "🌸 **Оставь контакт** 🌸\n\nНапиши @username или номер телефона.\n\nИли /cancel",
        reply_markup=ReplyKeyboardRemove()
    )
    await state.set_state(Dialogue.waiting_for_contact)

@dp.callback_query(lambda c: c.data == "not_ready")
async def handle_not_ready(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await callback.message.answer("🌿 Хорошо. Напиши /start, когда будешь готова 🌸", reply_markup=menu_keyboard)
    await state.set_state(Dialogue.chatting)

# ========== ЗАПУСК ==========
async def main():
    print("✨ Полноценный бот с подпиской, PDF-отчётами и всеми функциями запущен! ✨")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
