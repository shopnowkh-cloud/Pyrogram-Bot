import os
from pyrogram import Client, filters
from pyrogram.types import Message

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

app = Client(
    "simple_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)


@app.on_message(filters.command("start"))
async def start(client: Client, message: Message):
    await message.reply_text(
        f"សួស្តី {message.from_user.first_name}! 👋\n"
        "ខ្ញុំជា bot សាមញ្ញមួយ។\n\n"
        "បញ្ជា:\n"
        "/start - ចាប់ផ្តើម\n"
        "/help - ជំនួយ\n"
        "/echo <text> - ឆ្លើយត្រឡប់អ្វីដែលអ្នកនិយាយ"
    )


@app.on_message(filters.command("help"))
async def help_cmd(client: Client, message: Message):
    await message.reply_text(
        "📖 ជំនួយ:\n\n"
        "/start - ចាប់ផ្តើម bot\n"
        "/help - មើលបញ្ជានេះ\n"
        "/echo <text> - ឆ្លើយត្រឡប់សារ"
    )


@app.on_message(filters.command("echo"))
async def echo(client: Client, message: Message):
    text = message.text.split(None, 1)
    if len(text) < 2:
        await message.reply_text("Usage: /echo <text>")
        return
    await message.reply_text(text[1])


@app.on_message(filters.text & ~filters.command(["start", "help", "echo"]))
async def handle_text(client: Client, message: Message):
    await message.reply_text(f"អ្នកបាននិយាយថា: {message.text}")


if __name__ == "__main__":
    print("Bot កំពុងដំណើរការ...")
    app.run()
