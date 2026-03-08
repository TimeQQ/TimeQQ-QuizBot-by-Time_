import asyncio
import html
import logging
import sqlite3
from random import shuffle
from typing import List, Dict, Any
from urllib.parse import unquote

import aiohttp
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ---------- Конфигурация ----------
BOT_TOKEN = "ВАШ TOKEN"  # Замените на свой токен
DB_PATH = "quiz_bot.db"
API_URL = "https://opentdb.com/api.php"

# ---------- Инициализация бота и диспетчера ----------
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

logging.basicConfig(level=logging.INFO)

# ---------- Работа с базой данных ----------
def init_db():
    """Создаёт таблицу users, если её нет."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            total_score INTEGER DEFAULT 0,
            games_played INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()

def update_user_score(user_id: int, username: str, score_to_add: int):
    """Обновляет общий счёт пользователя и количество игр."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT total_score, games_played FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    if row:
        new_total = row[0] + score_to_add
        new_games = row[1] + 1
        cur.execute("UPDATE users SET total_score = ?, games_played = ?, username = ? WHERE user_id = ?",
                    (new_total, new_games, username, user_id))
    else:
        cur.execute("INSERT INTO users (user_id, username, total_score, games_played) VALUES (?, ?, ?, ?)",
                    (user_id, username, score_to_add, 1))
    conn.commit()
    conn.close()

def get_top_players(limit: int = 10) -> List[Dict[str, Any]]:
    """Возвращает список лучших игроков."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT username, total_score, games_played FROM users ORDER BY total_score DESC LIMIT ?", (limit,))
    rows = cur.fetchall()
    conn.close()
    return [{"username": r[0] or "Anonymous", "total_score": r[1], "games": r[2]} for r in rows]

# ---------- Функция для получения вопросов из OpenTrivia DB ----------
async def fetch_questions_from_api(amount: int = 10) -> List[Dict]:
    """
    Получает вопросы из opentdb.com.
    Возвращает список словарей с ключами: question, options, correct.
    """
    params = {
        "amount": amount,
        "type": "multiple",      
        "encode": "url3986"      
    }
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(API_URL, params=params) as resp:
                if resp.status != 200:
                    logging.error(f"API error: {resp.status}")
                    return []
                data = await resp.json()
                if data["response_code"] != 0:
                    logging.error(f"API response code: {data['response_code']}")
                    return []
                questions = []
                for item in data["results"]:
                    # Декодируем percent-encoding и HTML-сущности
                    question_text = html.unescape(unquote(item["question"]))
                    correct = html.unescape(unquote(item["correct_answer"]))
                    incorrect = [html.unescape(unquote(i)) for i in item["incorrect_answers"]]
                    # Формируем варианты и перемешиваем
                    options = [correct] + incorrect
                    shuffle(options)
                    correct_index = options.index(correct) + 1  
                    questions.append({
                        "question": question_text,
                        "options": options,
                        "correct": correct_index
                    })
                return questions
        except Exception as e:
            logging.error(f"Exception during API request: {e}")
            return []

# ---------- FSM состояния ----------
class QuizState(StatesGroup):
    playing = State()

# ---------- Хэндлеры ----------
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 Привет! Я бот-викторина с бесконечным запасом вопросов.\n"
        "Чтобы начать игру, отправь /quiz.\n"
        "Для просмотра таблицы лидеров используй /leaderboard."
    )

@dp.message(Command("quiz"))
async def cmd_quiz(message: types.Message, state: FSMContext):
    # Запрашиваем 10 вопросов из API
    questions = await fetch_questions_from_api(amount=10)
    if not questions:
        await message.answer("😕 Не удалось загрузить вопросы. Попробуйте позже.")
        return

    await state.set_state(QuizState.playing)
    await state.update_data(questions=questions, current=0, score=0)
    await send_question(message, state)

async def send_question(event: types.Message | CallbackQuery, state: FSMContext):
    """Отправляет текущий вопрос с инлайн-кнопками."""
    data = await state.get_data()
    questions = data["questions"]
    current = data["current"]

    if current >= len(questions):
        await finish_quiz(event, state)
        return

    q = questions[current]
    builder = InlineKeyboardBuilder()
    for i, opt in enumerate(q["options"], start=1):
        builder.button(text=opt, callback_data=f"answer_{i}")
    builder.adjust(1)

    text = f"Вопрос {current+1}/{len(questions)}:\n{q['question']}"

    if isinstance(event, types.Message):
        await event.answer(text, reply_markup=builder.as_markup())
    else:  # CallbackQuery
        await event.message.edit_text(text, reply_markup=builder.as_markup())
        await event.answer()

@dp.callback_query(QuizState.playing, F.data.startswith("answer_"))
async def process_answer(callback: CallbackQuery, state: FSMContext):
    user_answer = int(callback.data.split("_")[1])
    data = await state.get_data()
    questions = data["questions"]
    current = data["current"]
    score = data["score"]
    q = questions[current]

    correct = q["correct"]
    if user_answer == correct:
        score += 1
        await callback.answer("✅ Правильно!")
    else:
        correct_option = q["options"][correct-1]
        await callback.answer(f"❌ Неправильно. Правильный ответ: {correct_option}", show_alert=True)

    await state.update_data(current=current+1, score=score)
    await send_question(callback, state)

async def finish_quiz(event: types.Message | CallbackQuery, state: FSMContext):
    """Завершает игру, сохраняет результат."""
    data = await state.get_data()
    score = data["score"]
    total = len(data["questions"])
    user_id = event.from_user.id
    username = event.from_user.username or event.from_user.full_name

    update_user_score(user_id, username, score)

    text = f"🎉 Викторина завершена! Вы набрали {score} из {total} баллов."
    if isinstance(event, types.Message):
        await event.answer(text)
    else:
        await event.message.edit_text(text)
        await event.answer()

    await state.clear()

@dp.message(Command("leaderboard"))
async def cmd_leaderboard(message: types.Message):
    top = get_top_players(10)
    if not top:
        await message.answer("Таблица лидеров пока пуста. Сыграйте первым!")
        return

    lines = ["🏆 ТАБЛИЦА ЛИДЕРОВ 🏆", ""]
    for i, p in enumerate(top, 1):
        avg = p["total_score"] / p["games"] if p["games"] else 0
        lines.append(f"{i}. {p['username']} — {p['total_score']} очков (игр: {p['games']}, ср.: {avg:.1f})")
    await message.answer("\n".join(lines))

# ---------- Запуск ----------
async def main():
    init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())