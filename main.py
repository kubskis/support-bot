import asyncio
import logging
import os
from aiohttp import web
from aiogram import Bot, Dispatcher

# Настройка логирования
logging.basicConfig(level=logging.INFO)

# Новый токен бота
BOT_TOKEN = "8526419531:AAETEDjGFAC2FW5hHyaPCfuNivTNEiJTgVw"

# Простейший обработчик для Render Health Check (отвечает 200 OK)
async def handle_health_check(request):
    return web.Response(text="Bot Support ToH Secrets is running successfully!")

async def main():
    # Инициализация бота и диспетчера
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()

    # ПОДКЛЮЧИТЕ ВАШИ РОУТЕРЫ / ХЭНДЛЕРЫ ЗДЕСЬ:
    # Например: dp.include_router(your_router)

    # Создание и запуск HTTP-сервера для платформы Render
    app = web.Application()
    app.router.add_get("/", handle_health_check)
    app.router.add_get("/health", handle_health_check)

    runner = web.AppRunner(app)
    await runner.setup()

    # Чтение порта из переменной окружения Render (по умолчанию 8080)
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    print(f"Бот поддержки Tower Of Hell Secrets успешно запущен! HTTP-сервер слушает порт {port}.")

    # Запуск поллинга
    try:
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()

if __name__ == "__main__":
    asyncio.run(main())
    
