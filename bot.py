import os
import asyncio
import logging
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from pymongo import MongoClient
from dotenv import load_dotenv
from flask import Flask  # For Railway's web app requirement

# Load env vars
load_dotenv()
BOT_TOKEN = os.getenv('BOT_TOKEN')
MONGO_URI = os.getenv('MONGO_URI')
ADMIN_ID = int(os.getenv('ADMIN_ID'))
FORCE_SUB_CHANNELS = os.getenv('FORCE_SUB_CHANNELS').split(',') if os.getenv('FORCE_SUB_CHANNELS') else []  # List of channels
DELETE_TIME_MINUTES = int(os.getenv('DELETE_TIME_MINUTES', 30))

# MongoDB setup
client = MongoClient(MONGO_URI)
db = client['file_bot_db']
files_collection = db['files']  # Stores file metadata: {'file_id': str, 'link': str, 'revoked': bool, 'timestamp': datetime}

# Flask app for Railway (keeps it alive)
app = Flask(__name__)
@app.route('/')
def home():
    return "Bot is running!"

# Logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Helper: Check if user is subscribed to ALL channels
async def is_subscribed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user_id = update.effective_user.id
    for channel in FORCE_SUB_CHANNELS:
        try:
            member = await context.bot.get_chat_member(chat_id=channel, user_id=user_id)
            if member.status not in ['member', 'administrator', 'creator']:
                return False
        except:
            return False  # If channel doesn't exist or error, treat as not subscribed
    return True

# Helper: Generate unique link
def generate_link(file_id: str) -> str:
    return f"https://t.me/{context.bot.username}?start=file_{file_id}"

# /start command
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not await is_subscribed(update, context):
        # Create buttons for all channels
        keyboard = [[InlineKeyboardButton(f"Subscribe to {channel}", url=f"https://t.me/{channel[1:]}")] for channel in FORCE_SUB_CHANNELS]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("Please subscribe to all our channels to use this bot.", reply_markup=reply_markup)
        return
    
    # Check if start with file link
    if context.args and context.args[0].startswith('file_'):
        file_id = context.args[0].replace('file_', '')
        file_doc = files_collection.find_one({'file_id': file_id})
        if file_doc and not file_doc.get('revoked', False):
            await update.message.reply_text("Accessing file...")
            await context.bot.send_document(chat_id=user.id, document=file_doc['file_id'])  # Send file
        else:
            await update.message.reply_text("Link expired or invalid.")
    else:
        await update.message.reply_text(f"Hello {user.mention_html()}! Use /upload to share files (admin only).")

# /upload command (admin only)
async def upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Only admin can upload.")
        return
    await update.message.reply_text("Send me a file, photo, or video to upload.")

# Handle media (only admin can upload)
async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Only admin can upload files.")
        return
    
    message = update.message
    file_id = None
    if message.document:
        file_id = message.document.file_id
    elif message.photo:
        file_id = message.photo[-1].file_id  # Highest res
    elif message.video:
        file_id = message.video.file_id
    else:
        await update.message.reply_text("Unsupported file type.")
        return
    
    # Store in DB
    link = generate_link(file_id)
    files_collection.insert_one({
        'file_id': file_id,
        'link': link,
        'revoked': False,
        'timestamp': datetime.utcnow()
    })
    
    # Send link to admin
    keyboard = [[InlineKeyboardButton("Revoke Link", callback_data=f"revoke_{file_id}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(f"File uploaded! Share this link: {link}", reply_markup=reply_markup)
    
    # Schedule auto-delete
    context.job_queue.run_once(delete_message, DELETE_TIME_MINUTES * 60, data=message.message_id)

# Revoke link (callback from button)
async def revoke_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        await query.edit_message_text("Only admin can revoke.")
        return
    
    file_id = query.data.replace('revoke_', '')
    files_collection.update_one({'file_id': file_id}, {'$set': {'revoked': True}})
    await query.edit_message_text("Link revoked!")

# Auto-delete messages
async def delete_message(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.job
    try:
        await context.bot.delete_message(chat_id=context.bot.id, message_id=job.data)  # Delete bot's message
    except:
        pass  # Message might already be deleted

# Main function
def main() -> None:
    application = Application.builder().token(BOT_TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("upload", upload))
    application.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO | filters.VIDEO, handle_media))
    application.add_handler(CallbackQueryHandler(revoke_link, pattern="^revoke_"))
    
    # Run Flask in background for Railway
    from threading import Thread
    def run_flask():
        app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
    Thread(target=run_flask).start()
    
    application.run_polling()

if __name__ == '__main__':
    main()
