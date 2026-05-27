import asyncio
import logging
import os
import json
from datetime import datetime
from typing import Dict, Any

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
import gspread
from google.oauth2.service_account import Credentials
from openai import AsyncOpenAI

# ========== ЗАГРУЗКА ПЕРЕМЕННЫХ ==========
load_dotenv()
print(f"DEBUG: GROQ_API_KEY = {os.getenv('GROQ_API_KEY')}")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(id.strip()) for id in os.getenv("ADMIN_IDS", "").split(",") if id.strip()]
PSYCHOLOGIST_ID = int(os.getenv("PSYCHOLOGIST_ID", 0))
SHEET_ID = os.getenv("SHEET_ID")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")

# Проверка
if not GROQ_API_KEY:
    raise ValueError("GROQ_API_KEY не найден")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не найден")

# ========== ИНИЦИАЛИЗАЦИЯ ==========
groq_client = AsyncOpenAI(
    api_key=GROQ_API_KEY,
    base_url="https://api.groq.com/openai/v1"
)

PSYCHOLOGIST_NAME = "Дарья"

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# --- Состояния ---
class Dialogue(StatesGroup):
    chatting = State()
    waiting_for_contact = State()

# --- Кнопки меню ---
menu_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="ℹ️ Помощь")],
        [KeyboardButton(text="🗑 Очистить диалог"), KeyboardButton(text="❌ Отмена")]
    ],
    resize_keyboard=True
)

# --- Кнопка записи ---
book_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="📝 Записаться к Дарье", callback_data="book")],
        [InlineKeyboardButton(text="❌ Пока не готов", callback_data="not_ready")]
    ]
)

# --- Системный промпт ---
SYSTEM_PROMPT = f"""Ты — эмпатичный помощник-психолог по имени {PSYCHOLOGIST_NAME}.

Правила:
1. Внимательно слушай и задавай вопросы.
2. Проявляй эмпатию.
3. Не ставь диагнозы.
4. При кризисе — дай телефон доверия.
5. После 4-6 обменов предложи записаться к живому психологу.
6. В конце сообщения с предложением записи добавь фразу: "ЗАПИСЬ_ГОТОВА"

Отвечай коротко (2-4 предложения) на русском."""

# --- Хранилище ---
user_history = {}
user_problems = {}

def get_history(user_id: int):
    if user_id not in user_history:
        user_history[user_id] = [{"role": "system", "content": SYSTEM_PROMPT}]
    return user_history[user_id]

def detect_direction(text: str) -> str:
    text_lower = text.lower()
    keywords = {
        "тревога": ["тревог", "страх", "паник"],
        "отношения": ["отношени", "партнёр", "ссор"],
        "выгорание": ["выгоран", "устал", "нет сил"],
        "самооценка": ["самооценк", "неуверен"],
        "дети": ["ребёнк", "дочь", "сын"]
    }
    for direction, words in keywords.items():
        for word in words:
            if word in text_lower:
                return direction
    return "общая поддержка"

# --- Google Sheets ---
def get_sheet():
    try:
        creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
        creds = Credentials.from_service_account_info(creds_dict, scopes=["https://www.googleapis.com/auth/spreadsheets"])
        client = gspread.authorize(creds)
        return client.open_by_key(SHEET_ID).sheet1
    except Exception as e:
        logging.error(f"Ошибка Google Sheets: {e}")
        return None

def save_to_sheet(user_id: int, username: str, problem: str, direction: str, contact: str):
    sheet = get_sheet()
    if not sheet:
        return False
    try:
        if not sheet.get_all_values():
            headers = ["Timestamp", "User ID", "Username", "Problem", "Direction", "Contact", "Status"]
            sheet.append_row(headers)
        row = [datetime.now().strftime("%Y-%m-%d %H:%M:%S"), user_id, username, problem[:200], direction, contact, "new"]
        sheet.append_row(row)
        return True
    except Exception as e:
        logging.error(f"Ошибка сохранения: {e}")
        return False

# --- Уведомление психологу ---
async def notify_psychologist(user_id: int, username: str, problem: str, direction: str, contact: str):
    message = (
        f"🔔 **НОВЫЙ ЗАПРОС**\n\n"
        f"👤 {username} (ID: {user_id})\n"
        f"📝 {problem[:300]}\n"
        f"🏷 {direction}\n"
        f"📞 {contact}"
    )
    if PSYCHOLOGIST_ID:
        try:
            await bot.send_message(PSYCHOLOGIST_ID, message, parse_mode="Markdown")
        except Exception as e:
            logging.error(f"Ошибка: {e}")

# --- Обработчики ---
@dp.callback_query(lambda c: c.data == "book")
async def handle_book(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.answer(
        "📝 **Оставь контакт**\n\nНапиши @username или номер телефона.",
        reply_markup=ReplyKeyboardRemove()
    )
    await state.set_state(Dialogue.waiting_for_contact)

@dp.callback_query(lambda c: c.data == "not_ready")
async def handle_not_ready(callback: types.CallbackQuery):
    await callback.answer()
    await callback.message.answer("Хорошо, я здесь если захочешь поговорить. Напиши /start", reply_markup=menu_keyboard)

@dp.message(StateFilter(Dialogue.waiting_for_contact))
async def process_contact(message: types.Message, state: FSMContext):
    contact = message.text
    user_id = message.from_user.id
    username = message.from_user.username or "None"
    problem_info = user_problems.get(user_id, {"problem": "Диалог с ИИ", "direction": "не определено"})
    
    save_to_sheet(user_id, username, problem_info["problem"], problem_info["direction"], contact)
    await notify_psychologist(user_id, username, problem_info["problem"], problem_info["direction"], contact)
    
    if user_id in user_history:
        del user_history[user_id]
    if user_id in user_problems:
        del user_problems[user_id]
    
    await message.answer(f"✅ Спасибо! Психолог {PSYCHOLOGIST_NAME} свяжется с вами в течение 24 часов. Берегите себя ❤️", reply_markup=menu_keyboard)
    await state.clear()

# --- Команды ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    await state.clear()
    if user_id in user_history:
        del user_history[user_id]
    if user_id in user_problems:
        del user_problems[user_id]
    
    await message.answer(
        f"👋 **Привет! Я помощник психолога {PSYCHOLOGIST_NAME}.**\n\nРасскажи, что тебя беспокоит.",
        reply_markup=menu_keyboard,
        parse_mode="Markdown"
    )
    await state.set_state(Dialogue.chatting)

@dp.message(Command("reset"))
async def cmd_reset(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    if user_id in user_history:
        del user_history[user_id]
    if user_id in user_problems:
        del user_problems[user_id]
    await message.answer("🔄 История очищена.", reply_markup=menu_keyboard)

@dp.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Отменено.", reply_markup=menu_keyboard)

@dp.message(F.text == "ℹ️ Помощь")
async def menu_help(message: types.Message):
    await message.answer("📋 /start - начать\n/reset - очистить\n/cancel - отменить\n\nПросто расскажи, что тебя беспокоит.")

@dp.message(F.text == "🗑 Очистить диалог")
async def menu_reset(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    if user_id in user_history:
        del user_history[user_id]
    if user_id in user_problems:
        del user_problems[user_id]
    await message.answer("🗑 История очищена.", reply_markup=menu_keyboard)

@dp.message(F.text == "❌ Отмена")
async def menu_cancel(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state == Dialogue.waiting_for_contact:
        await state.clear()
        await message.answer("❌ Запись отменена.", reply_markup=menu_keyboard)
    else:
        await message.answer("❌ Нет активных действий.", reply_markup=menu_keyboard)

# --- Основной диалог ---
@dp.message(Dialogue.chatting)
async def chat_with_ai(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    user_text = message.text
    
    if user_text in ["ℹ️ Помощь", "🗑 Очистить диалог", "❌ Отмена"]:
        return
    
    # Кризисная проверка
    crisis = ["суицид", "самоубийств", "не хочу жить"]
    if any(word in user_text.lower() for word in crisis):
        await message.answer("🚨 Телефон доверия: 8-800-2000-122. Пожалуйста, позвоните ❤️")
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
            await message.answer(f"💬 Хотите обсудить это с психологом {PSYCHOLOGIST_NAME}?", reply_markup=book_keyboard)
        else:
            await message.answer(answer)
        
    except Exception as e:
        print(f"Ошибка: {e}")
        await message.answer("Ошибка. Попробуйте позже.")

# --- Запуск ---
async def main():
    print("🚀 Бот запускается...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
