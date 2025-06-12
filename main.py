import asyncio
from bot import run_telegram_bot, app  # Import app instance
from trade_monitor import monitor_trades
from config import MONITORING
from telegram_utils import send_telegram_message

async def main():
    """Run both the Telegram bot and trade monitoring concurrently."""
    bot_task = asyncio.create_task(run_telegram_bot())  # Run the bot without blocking
    # asyncio.create_task(monitor_trades(app)) ##### REMOVE THIS WHEN BOT FINSISHES
    if MONITORING:
        asyncio.create_task(monitor_trades(app))  # Start trade monitoring

    try:
        await asyncio.gather(bot_task)  # This will handle exceptions from bot_task
    except Exception as e:
        print(f"Exception in bot task: {e}")
    finally:
        if app:  # Check if app was initialized
            await app.shutdown()  # Ensure bot shutdown

if __name__ == "__main__":
    
    asyncio.run(main())  # Use asyncio.run for cleaner event loop handling