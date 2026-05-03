import os
from pyrogram import Client, filters

app = Client("simple_bot",
    api_id=int(os.environ["TELEGRAM_API_ID"]),
    api_hash=os.environ["TELEGRAM_API_HASH"],
    bot_token=os.environ["TELEGRAM_BOT_TOKEN"])

@app.on_message(filters.command("start"))
async def start(_, m):
    await m.reply(f"សួស្តី {m.from_user.first_name}! 👋\nបញ្ជា: /start /help /echo")

@app.on_message(filters.command("help"))
async def help_cmd(_, m):
    await m.reply("/start - ចាប់ផ្តើម\n/help - ជំនួយ\n/echo <text> - ឆ្លើយត្រឡប់")

@app.on_message(filters.command("echo"))
async def echo(_, m):
    parts = m.text.split(None, 1)
    await m.reply(parts[1] if len(parts) > 1 else "Usage: /echo <text>")

@app.on_message(filters.text & ~filters.command(["start", "help", "echo"]))
async def handle(_, m):
    await m.reply(m.text)

app.run()
