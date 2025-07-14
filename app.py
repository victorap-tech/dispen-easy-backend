from flask import Flask, request, jsonify
import sqlite3
import requests
import os

app = Flask(__name__)

# Inicializa la base de datos
def init_db():
    conn = sqlite3.connect('pagos.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS pagos (
            id_pago TEXT PRIMARY KEY,
            estado TEXT
        )
    ''')
    conn.commit()
    conn.close()

# Webhook de MercadoPago (llamado automático cuando alguien paga)
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json()
    if not data or 'data' not in data or 'id' not in data['data']:
        return 'Payload inválido', 400

    id_pago = str(data['data']['id'])  # ID de pago recibido

    # Token de acceso a la API de MercadoPago (modo TEST o producción)
    access_token = 'APP_USR-7903926381447246-061121-b38fe6b7c7d58e0b3927c08d041e9bd9-246749043'  # ← Reemplazá por tu token real

    headers = {
        "Authorization": f"Bearer {access_token}"
    }

    # Consultar el estado real del pago a MercadoPago
    response = requests.get(f"https://api.mercadopago.com/v1/payments/{id_pago}", headers=headers)

    if response.status_code != 200:
        return 'No se pudo consultar el estado del pago', 400

    payment_info = response.json()
    estado = payment_info.get('status', 'pendiente')  # 'approved', 'rejected', etc.

    # Guardar en la base de datos local
    conn = sqlite3.connect('pagos.db')
    cursor = conn.cursor()
    cursor.execute('INSERT OR REPLACE INTO pagos (id_pago, estado) VALUES (?, ?)', (id_pago, estado))
    conn.commit()
    conn.close()

    return 'OK', 200

# Consulta del estado de un pago (usado por ESP32)
@app.route('/check_payment', methods=['GET'])
def check_payment():
    id_pago = request.args.get('id_pago')
    if not id_pago:
        return jsonify({'error': 'Falta el parámetro id_pago'}), 400

    conn = sqlite3.connect('pagos.db')
    cursor = conn.cursor()
    cursor.execute('SELECT estado FROM pagos WHERE id_pago = ?', (id_pago,))
    row = cursor.fetchone()
    conn.close()

    if row:
        return jsonify({'estado': row[0]})
    else:
        return jsonify({'estado': 'no_encontrado'})

# Inicializa la base de datos
init_db()

# Puerto dinámico para Railway
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)




     
  
