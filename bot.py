import os
import logging
from pyrogram import Client, filters
from pyrogram.types import Message

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

app = Client(
    "simple_bot",
    api_id=int(os.environ["TELEGRAM_API_ID"]),
    api_hash=os.environ["TELEGRAM_API_HASH"],
    bot_token=os.environ["TELEGRAM_BOT_TOKEN"],
)


@app.on_message(filters.command("start"))
async def cmd_start(_, m: Message):
    logger.info(f"[/start] from user_id={m.from_user.id}")
    await m.reply("សួស្ដី! 👋 Bot កំពុង​ដំណើរការ​ជាធម្មតា។")


@app.on_message(filters.text & ~filters.command(["start"]))
async def handle_text(_, m: Message):
    logger.info(f"[text] from user_id={m.from_user.id} text={m.text!r}")
    await m.reply("សួស្តី")


logger.info("Starting bot...")
app.run()
