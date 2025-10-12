import os
import requests

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def enviar_mensaje_telegram(mensaje, chat_id=None):
    """
    Envía un mensaje al chat de Telegram especificado.
    Si no se pasa chat_id, usa el de administrador (TELEGRAM_CHAT_ID).
    """
    if not TELEGRAM_BOT_TOKEN:
        print("⚠️ TELEGRAM_BOT_TOKEN no configurado.")
        return False

    destino = chat_id or TELEGRAM_CHAT_ID
    if not destino:
        print("⚠️ TELEGRAM_CHAT_ID no configurado.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {
        "chat_id": destino,
        "text": mensaje,
        "parse_mode": "HTML"
    }

    try:
        resp = requests.post(url, json=data)
        if resp.status_code == 200:
            print(f"✅ Mensaje enviado a {destino}")
            return True
        else:
            print(f"❌ Error enviando mensaje: {resp.text}")
            return False
    except Exception as e:
        print(f"❌ Excepción enviando mensaje a Telegram: {e}")
        return False
