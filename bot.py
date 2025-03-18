import json
import logging
import os
import asyncio
from pathlib import Path
from typing import Dict, List

# Импорт компонентов aiogram для работы с Telegram API
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import KeyboardButton, ReplyKeyboardMarkup, FSInputFile
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder

# Загрузка переменных окружения и настройка OpenAI
from dotenv import load_dotenv
from openai import OpenAI
from openai import RateLimitError

# Константы приложения
MAX_TASK_NUMBER = 20          # Максимальный номер задания ЕГЭ
TASKS_PER_PAGE = 5            # Количество кнопок с заданиями в ряду
GPT_MODEL ="deepseek/deepseek-r1:free"
THEORY_DIR = Path(__file__).parent / "theory"       # Путь к теоретическим материалам

# Настройка системы логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Загрузка переменных окружения из .env файла
load_dotenv()

class BotStates(StatesGroup):
    """Класс состояний конечного автомата для управления диалогом"""
    choosing_task = State()    # Состояние выбора номера задания
    solving_task = State()     # Состояние решения конкретной задачи

class PhysicsBot:
    """Основной класс бота для подготовки к ЕГЭ по физике"""
    
    def __init__(self):
        """Инициализация компонентов бота"""
        self.bot = Bot(token=self._get_env("BOT_TOKEN"))
        self.dp = Dispatcher()
        
        # Настройка клиента OpenAI с использованием OpenRouter
        self.openai_client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=self._get_env("OPENAI_API_KEY")
        )
        
        self.tasks_db = self._load_tasks()  # Загрузка базы задач
        
        # Регистрация обработчиков
        self.dp.message.register(self.start_command, Command("start"))
        self.dp.message.register(self.handle_text_responses)
        self.dp.callback_query.register(self.handle_callbacks)

    @staticmethod
    def _get_env(key: str) -> str:
        """Получение переменных окружения с проверкой"""
        value = os.getenv(key)
        if not value:
            raise ValueError(f"Missing {key} in environment")
        return value

    def _load_tasks(self) -> Dict[int, List[Dict]]:
        """Загрузка задач из JSON-файла"""
        with open('tasks.json', 'r', encoding='utf-8') as f:
            return {int(k): v for k, v in json.load(f).items()}

    async def start_command(self, message: types.Message):
        """Обработка команды /start"""
        logger.info(f"User {message.from_user.id} started bot")
        await message.answer(
            f"Привет, {message.from_user.full_name}!\nНачинаем подготовку к ЕГЭ?",
            reply_markup=self._start_keyboard()
        )

    async def handle_text_responses(self, message: types.Message, state: FSMContext):
        """Центральный обработчик текстовых сообщений"""
        text = message.text.strip().lower()
        current_state = await state.get_state()
        
        # Обработка основных сценариев
        if text == "да, поехали)":
            await self.start_preparation(message, state)
        elif text == "нет, не хочу(":
            await self.cancel_preparation(message)
        elif text == "я хочу подготовиться":
            await self.restart_preparation(message, state)
        elif text.isdigit() and current_state == BotStates.choosing_task:
            await self.handle_task_number(message, state)
        elif current_state == BotStates.solving_task:
            await self.handle_task_answer(message, state)
        else:
            await message.answer("Не понимаю команду 😢 Используйте кнопки меню")

    async def handle_callbacks(self, query: types.CallbackQuery, state: FSMContext):
        """Обработчик inline-кнопок"""
        await query.answer()
        action = query.data
        
        # Маршрутизация действий
        handlers = {
            "play": self.start_solving_tasks,
            "mainmenu": self.return_to_main_menu,
            "gpt": self.provide_gpt_help
        }
        
        if action in handlers:
            await handlers[action](query, state)
        else:
            await query.message.answer("Неизвестная команда")

    def _start_keyboard(self) -> ReplyKeyboardMarkup:
        """Главное меню с базовыми опциями"""
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="Да, поехали)")],
                [KeyboardButton(text="Нет, не хочу(")]
            ],
            resize_keyboard=True,
            input_field_placeholder="Выберите вариант ответа"
        )

    def _task_numbers_keyboard(self) -> ReplyKeyboardMarkup:
        """Клавиатура с номерами заданий"""
        builder = ReplyKeyboardBuilder()
        for i in range(1, MAX_TASK_NUMBER + 1):
            builder.add(KeyboardButton(text=str(i)))
        builder.adjust(TASKS_PER_PAGE)
        return builder.as_markup(resize_keyboard=True)

    async def start_preparation(self, message: types.Message, state: FSMContext):
        """Начало процесса подготовки"""
        logger.info(f"User {message.from_user.id} started preparation")
        await state.set_state(BotStates.choosing_task)
        await message.answer(
            "Отличный выбор!\nКакое задание повторим?",
            reply_markup=self._task_numbers_keyboard()
        )

    async def handle_task_number(self, message: types.Message, state: FSMContext):
        """Обработка выбора номера задания"""
        task_num = int(message.text)
        
        # Валидация введенного номера
        if not 1 <= task_num <= MAX_TASK_NUMBER:
            await message.answer(f"Выберите задание от 1 до {MAX_TASK_NUMBER}")
            return

        await self._send_theory_materials(message, task_num)
        await state.update_data(current_task=task_num, current_problem=0)
        await self._offer_problem_solving(message)

    async def _send_theory_materials(self, message: types.Message, task_num: int):
        """Отправка теоретических материалов для задания"""
        try:
            files = [
                THEORY_DIR / f"{task_num}.docx",
                THEORY_DIR / f"{task_num}.pdf"
            ]
            
            sent = False
            for file in files:
                if file.exists():
                    await message.answer_document(
                        FSInputFile(file),
                        caption=f"📚 Теория по заданию {task_num}",
                        reply_markup=types.ReplyKeyboardRemove()
                    )
                    sent = True
                    break
            
            if not sent:
                await message.answer("❌ Материалы для этого задания еще не готовы")
        except Exception as e:
            logger.error(f"File send error: {str(e)}")
            await message.answer("⚠️ Ошибка при отправке файла")

    async def _offer_problem_solving(self, message: types.Message):
        """Предложение начать решать задачи"""
        builder = InlineKeyboardBuilder()
        builder.row(types.InlineKeyboardButton(
            text="📝 Решать задачи", 
            callback_data="play"
        ))
        builder.row(types.InlineKeyboardButton(
            text="🔄 Выбрать другое задание", 
            callback_data="mainmenu"
        ))
        
        await message.answer(
            "Хочешь порешать задания?", 
            reply_markup=builder.as_markup()
        )

    async def start_solving_tasks(self, query: types.CallbackQuery, state: FSMContext):
        """Запуск решения задач выбранного задания"""
        await state.set_state(BotStates.solving_task)
        await self._send_next_problem(query.message.chat.id, state)

    async def _send_next_problem(self, chat_id: int, state: FSMContext):
        """Отправка следующей задачи в очереди"""
        data = await state.get_data()
        task_num = data.get('current_task')
        problem_idx = data.get('current_problem', 0)
        
        # Проверка наличия данных о задании
        if not task_num or task_num not in self.tasks_db:
            await state.clear()
            return

        problems = self.tasks_db[task_num]
        
        # Проверка завершения всех задач
        if problem_idx >= len(problems):
            await self.bot.send_message(
                chat_id,
                "🎉 Все задачи решены!",
                reply_markup=self._start_keyboard()
            )
            await state.clear()
            return

        problem = problems[problem_idx]
        builder = InlineKeyboardBuilder()
        builder.row(types.InlineKeyboardButton(
            text="Помощь ChatGPT", 
            callback_data="gpt"
        ))
        builder.row(types.InlineKeyboardButton(
            text="🔄 Сменить задание", 
            callback_data="mainmenu"
        ))

        await self.bot.send_message(
            chat_id,
            f"Задача {problem_idx+1}/{len(problems)}:\n{problem['question']}",
            reply_markup=builder.as_markup()
        )

    async def handle_task_answer(self, message: types.Message, state: FSMContext):
        """Проверка ответа пользователя"""
        data = await state.get_data()
        task_num = data.get('current_task')
        problem_idx = data.get('current_problem', 0)
        
        if not task_num or task_num not in self.tasks_db:
            await state.clear()
            return

        problems = self.tasks_db[task_num]
        problem = problems[problem_idx]
        user_answer = message.text.strip().lower()
        correct_answer = problem['answer'].lower()

        # Сравнение ответов
        if user_answer == correct_answer:
            await message.answer("✅ Верно! Отличная работа!")
            await state.update_data(current_problem=problem_idx + 1)
            await self._send_next_problem(message.chat.id, state)
        else:
            await message.answer("❌ Неверно. Попробуйте еще раз или запросите помощь")

    async def return_to_main_menu(self, query: types.CallbackQuery, state: FSMContext):
        """Возврат в главное меню"""
        await state.set_state(BotStates.choosing_task)
        await query.message.answer(
            "Выберите задание:",
            reply_markup=self._task_numbers_keyboard()
        )

    async def provide_gpt_help(self, query: types.CallbackQuery, state: FSMContext):
        """Генерация объяснения через GPT"""
        await query.message.answer("🕒 Генерируем объяснение...")
        
        try:
            data = await state.get_data()
            task_num = data.get('current_task')
            problem_idx = data.get('current_problem', 0)
            
            if not task_num or task_num not in self.tasks_db:
                await query.message.answer("⚠️ Сначала выберите задание")
                return

            problem = self.tasks_db[task_num][problem_idx]
            prompt = self._build_gpt_prompt(problem)
            
            response = await asyncio.wait_for(
                self._get_gpt_response(prompt),
                timeout=15
            )

            if response:
                await self._send_gpt_response(
                    query.message,
                    response,
                    problem['answer']
                )
            else:
                await query.message.answer("Ответ не формируется. Попробуйте еще раз позже")
            
        except RateLimitError:
            await query.message.answer("⚠️ Превышен лимит запросов. Попробуйте еще раз позже.")
        except asyncio.TimeoutError:
            await query.message.answer("⚠️ Таймаут запроса. Попробуйте еще раз позже.")
        except Exception as e:
            logger.error(f"GPT error: {str(e)}")
            await query.message.answer("⚠️ Временные сбои\nНажмите \"Помощь ChatGPT\" еще раз")

    def _build_gpt_prompt(self, problem: Dict) -> str:
        """Формирование промпта для GPT"""
        return (
            f"Реши задачу ЕГЭ по физике. Требования:\n"
            f"1. Пошаговое объяснение\n"
            f"2. Использование формул\n"
            f"3. Логические выводы\n"
            f"4. Ответ должен быть кратким и не содержать проблемных символов, он будет перенаправлен в телеграм, который может не обработать символы (неправильно форматироваться)\n"
            f"5. Окончательный ответ: {problem['answer']}\n\n"
            f"Задача: {problem['question']}"
        )

    async def _get_gpt_response(self, prompt: str) -> str:
        """Получение ответа от GPT API с повторными попытками"""
        retries = 5  # Количество попыток
        for i in range(retries):
            try:
                response = self.openai_client.chat.completions.create(
                    model=GPT_MODEL,
                    messages=[{"role": "user", "content": prompt}]
                )

                if response and response.choices:
                    return response.choices[0].message.content
            except Exception as e:
                logger.error(f"Ошибка при запросе к GPT: {str(e)}")
                await asyncio.sleep(2)  # Пауза перед следующей попыткой

        # Если после всех попыток не удалось получить ответ, возвращаем None
        return None


    async def _send_gpt_response(self, message: types.Message, response: str, correct_answer: str):
        """Форматирование и отправка ответа GPT"""
        formatted_response = (
            f"🧠 Объяснение от ChatGPT\n\n"
            f"{response}\n\n"
            f"✅ Правильный ответ: {correct_answer}\n"
            f"🔍 Примечание: Ответ сгенерирован ИИ, который может ошибаться. Всегда проверяйте вычисления"
        )
        
        builder = InlineKeyboardBuilder()
        builder.row(types.InlineKeyboardButton(
            text="🔄 Сменить задание", 
            callback_data="mainmenu"
        ))
        
        await message.answer(
            formatted_response,
            parse_mode="HTML",
            reply_markup=builder.as_markup()
        )
        await message.answer("Введите правильный ответ для продолжения:")

    async def cancel_preparation(self, message: types.Message):
        """Обработка отказа от подготовки"""
        await message.answer(
            "Будем ждать вас снова!",
            reply_markup=ReplyKeyboardMarkup(
                keyboard=[[KeyboardButton(text="Я хочу подготовиться")]],
                resize_keyboard=True
            )
        )

    async def restart_preparation(self, message: types.Message, state: FSMContext):
        """Перезапуск процесса подготовки"""
        await state.set_state(BotStates.choosing_task)
        await message.answer(
            "Выбирайте задание:",
            reply_markup=self._task_numbers_keyboard()
        )

    async def run(self):
        """Запуск бота"""
        await self.bot.delete_webhook(drop_pending_updates=True)
        await self.dp.start_polling(self.bot)

if __name__ == '__main__':
    try:
        bot = PhysicsBot()
        asyncio.run(bot.run())
    except Exception as e:
        logger.critical(f"Bot startup failed: {str(e)}")