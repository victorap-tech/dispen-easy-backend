from flask import Flask, request, jsonify
import sqlite3
from datetime import datetime

app = Flask(__name__)
DATABASE = 'pagos.db'

# Inicializa la base de datos
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

# Ruta del webhook
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json
    if data and "data" in data and "id" in data["data"]:
        id_pago = str(data["data"]["id"])
        estado = "aprobado"
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with sqlite3.connect(DATABASE) as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT INTO pagos (id_pago, estado, timestamp) VALUES (?, ?, ?)", (id_pago, estado, timestamp))
            conn.commit()
        return jsonify({"status": "success"}), 200
    return jsonify({"status": "invalid payload"}), 400

# Consulta de pago pendiente (para ESP32)
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

# Marcar como dispensado
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
    app.run(host='0.0.0.0', port=5000, debug=True)
