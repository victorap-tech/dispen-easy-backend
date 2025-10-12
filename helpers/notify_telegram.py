import os
import requests
from flask import current_app

def notify_telegram(message, dispenser_id=None):
    try:
        from app import db, OperatorToken  # Importa directamente desde app.py
    except Exception as e:
        print("⚠️ Error importando modelos desde app:", e)
        return

    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    admin_chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not bot_token or not admin_chat_id:
        print("⚠️ TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID no configurados")
        return

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    # Enviar al admin principal
    try:
        requests.post(url, json={"chat_id": admin_chat_id, "text": message})
        print("✅ Mensaje enviado al admin")
    except Exception as e:
        print("⚠️ Error enviando al admin:", e)

    # Enviar al operador vinculado al dispenser (si existe)
    if dispenser_id:
        try:
            operator = OperatorToken.query.filter_by(dispenser_id=dispenser_id, activo=True).first()
            if operator and operator.chat_id:
                requests.post(url, json={"chat_id": operator.chat_id, "text": message})
                print(f"✅ Mensaje enviado al operador {operator.nombre}")
        except Exception as e:
            print("⚠️ Error enviando al operador:", e)
