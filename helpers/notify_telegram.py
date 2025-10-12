import os
import requests
from models import OperatorToken  # importa tu modelo

def notify_telegram(message, dispenser_id=None):
    """
    Envía un mensaje de Telegram al administrador y al operador vinculado (si lo hay).
    Se usa para avisos de estado, stock bajo, recargas, etc.
    """
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    admin_chat = os.getenv("TELEGRAM_CHAT_ID")

    if not bot_token:
        print("⚠️ TELEGRAM_BOT_TOKEN no configurado en variables de entorno")
        return False

    # --- 1️⃣ Enviar al administrador principal ---
    if admin_chat:
        try:
            requests.get(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                params={"chat_id": admin_chat, "text": message},
                timeout=5
            )
            print(f"📨 Notificación enviada al admin: {admin_chat}")
        except Exception as e:
            print(f"❌ Error al enviar al admin: {e}")

    # --- 2️⃣ Enviar al operador vinculado al dispenser (si existe) ---
    if dispenser_id:
        try:
            op = OperatorToken.query.filter_by(dispenser_id=dispenser_id, activo=True).first()
            if op and op.chat_id:
                requests.get(
                    f"https://api.telegram.org/bot{bot_token}/sendMessage",
                    params={"chat_id": op.chat_id, "text": message},
                    timeout=5
                )
                print(f"📨 Notificación enviada al operador {op.nombre} ({op.chat_id})")
            else:
                print("⚠️ No hay operador activo vinculado a este dispenser")
        except Exception as e:
            print(f"❌ Error al enviar al operador: {e}")

    return True
