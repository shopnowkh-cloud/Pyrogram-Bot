import os
from pyrogram import Client, filters

app = Client("simple_bot",
    api_id=int(os.environ["TELEGRAM_API_ID"]),
    api_hash=os.environ["TELEGRAM_API_HASH"],
    bot_token=os.environ["TELEGRAM_BOT_TOKEN"])

@app.on_message(filters.text)
async def handle(_, m):
    await m.reply("សួស្តី")

app.run()
