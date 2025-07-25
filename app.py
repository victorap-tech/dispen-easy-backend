from flask import Flask, request, jsonify
from flask_cors import CORS
import sqlite3
import requests
import os
from datetime import datetime

app = Flask(__name__)
CORS(app)  # Habilita CORS para que el ESP32 o apps externas puedan consumirlo

ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")  # Se recomienda definirlo como variable de entorno
DATABASE = 'pagos.db'

# Inicializa base de datos
def init_db():
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS pagos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                id_pago TEXT,
                estado TEXT,
                timestamp TEXT,
                dispensado INTEGER DEFAULT 0
            )
        ''')
        conn.commit()

# Ruta de prueba
@app.route('/')
def home():
    return "Backend Dispen-Easy operativo."

# Webhook de MercadoPago
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json
    print("[WEBHOOK] Datos recibidos:", data)

    if data and "data" in data and "id" in data["data"]:
        id_pago = str(data["data"]["id"])
        estado = "aprobado"  # Por ahora asumimos que si llega es aprobado
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        with sqlite3.connect(DATABASE) as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT INTO pagos (id_pago, estado, timestamp) VALUES (?, ?, ?)",
                           (id_pago, estado, timestamp))
            conn.commit()
        return jsonify({"status": "success"}), 200

    return jsonify({"status": "invalid payload"}), 400

# ESP32 consulta si hay pagos pendientes
@app.route('/check_payment_pendiente', methods=['GET'])
def check_payment_pendiente():
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id_pago FROM pagos WHERE estado = 'aprobado' AND dispensado = 0 ORDER BY timestamp LIMIT 1")
        row = cursor.fetchone()
        if row:
            return jsonify({"pendiente": True, "id_pago": row[0]})
        else:
            return jsonify({"pendiente": False})

# Marcar pago como ya usado por ESP32
@app.route('/marcar_dispensado', methods=['POST'])
def marcar_dispensado():
    data = request.json
    id_pago = data.get("id_pago")

    if id_pago:
        with sqlite3.connect(DATABASE) as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE pagos SET dispensado = 1 WHERE id_pago = ?", (id_pago,))
            conn.commit()
        return jsonify({"status": "ok"})
    return jsonify({"status": "missing id_pago"}), 400

if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=5000)
