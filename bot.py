# bot.py — Bot de vinculación Dispen-Easy
import os, requests
from telegram.ext import Application, CommandHandler, MessageHandler, filters

# 🔧 Configuración
BACKEND = os.getenv("BACKEND_BASE_URL", "https://web-production-e7d2.up.railway.app").rstrip("/")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

async def start(update, context):
    """Manejo de /start <TOKEN>"""
    chat_id = update.effective_chat.id
    args = context.args
    if args:
        token = args[0]
        try:
            r = requests.post(f"{BACKEND}/api/operator/link",
                              json={"token": token, "chat_id": str(chat_id)}, timeout=10)
            ok = r.ok and (r.json().get("ok") is True)
            await update.message.reply_text("✅ Token vinculado. Recibirás alertas de stock." if ok else "❌ Token inválido.")
        except Exception:
            await update.message.reply_text("⚠️ No pude vincular. Probá más tarde.")
    else:
        await update.message.reply_text("Enviame tu token o usá /start <TOKEN> para vincular.")

async def plain_token(update, context):
    """Si el operador pega solo el token"""
    token = (update.message.text or "").strip()
    if len(token) < 6:
        return
    chat_id = update.effective_chat.id
    try:
        r = requests.post(f"{BACKEND}/api/operator/link",
                          json={"token": token, "chat_id": str(chat_id)}, timeout=10)
        ok = r.ok and (r.json().get("ok") is True)
        await update.message.reply_text("✅ Token vinculado." if ok else "❌ Token inválido.")
    except Exception:
        await update.message.reply_text("⚠️ Error vinculando. Intentá más tarde.")

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, plain_token))
    print("🤖 Bot Dispen-Easy conectado y escuchando mensajes...")
    app.run_polling()

if __name__ == "__main__":
    main()
