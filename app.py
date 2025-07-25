from flask import Flask, request, jsonify
import requests

app = Flask(__name__)

# Token de producción de MercadoPago (reemplazar con el tuyo)
ACCESS_TOKEN = 'APP_USR-7903926381447246-061121-b38fe6b7c7d58e0b3927c08d041e9bd9-246749043'

@app.route('/')
def home():
    return 'Servidor Dispen-Easy Link Generator Activo'

# Endpoint para crear link de pago desde frontend
@app.route('/crear_link', methods=['POST'])
def crear_link():
    data = request.json
    print("Datos recibidos:", data)

    title = data.get('title')
    quantity = data.get('quantity', 1)
    unit_price = data.get('unit_price')

    if not all([title, unit_price]):
        return jsonify({"error": "Datos incompletos"}), 400

    payload = {
        "items": [
            {
                "title": title,
                "quantity": quantity,
                "unit_price": float(unit_price),
                "currency_id": "ARS"
            }
        ],
        "notification_url": "https://TU_DOMINIO/webhook",
        "back_urls": {
            "success": "https://www.success.com",
            "failure": "https://www.failure.com",
            "pending": "https://www.pending.com"
        },
        "auto_return": "approved"
    }

    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }

    response = requests.post("https://api.mercadopago.com/checkout/preferences", json=payload, headers=headers)

    if response.status_code == 201:
        return jsonify(response.json())
    else:
        return jsonify({"error": "Fallo al crear preferencia", "detalle": response.text}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
