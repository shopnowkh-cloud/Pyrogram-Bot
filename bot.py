import os
import asyncio
import signal
from pyrogram import Client, filters
from pyrogram.types import Message

app = Client(
    "simple_bot",
    api_id=int(os.environ["TELEGRAM_API_ID"]),
    api_hash=os.environ["TELEGRAM_API_HASH"],
    bot_token=os.environ["TELEGRAM_BOT_TOKEN"],
)


@app.on_message(filters.text)
async def handle(_, m: Message):
    await m.reply("សួស្តី")


async def main():
    await app.start()
    print("Bot is running...")

    stop_event = asyncio.Event()

    def _stop(sig, frame):
        print(f"Received signal {sig}, shutting down...")
        stop_event.set()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    await stop_event.wait()
    await app.stop()
    print("Bot stopped.")


if __name__ == "__main__":
    asyncio.run(main())
