from flask import Flask, request, jsonify
import sqlite3
from datetime import datetime

app = Flask(__name__)

DB_PATH = "pagos.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS pagos (
                    id_pago TEXT PRIMARY KEY,
                    estado TEXT,
                    dispensado INTEGER DEFAULT 0,
                    fecha TEXT
                )''')
    conn.commit()
    conn.close()

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json()
    id_pago = data.get("id_pago")
    estado = data.get("estado")

    if id_pago and estado:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO pagos (id_pago, estado, dispensado, fecha) VALUES (?, ?, ?, ?)",
                  (id_pago, estado, 0, datetime.now().isoformat()))
        conn.commit()
        conn.close()
        return jsonify({"status": "ok"}), 200
    return jsonify({"error": "Datos incompletos"}), 400

@app.route('/check_payment_pendiente', methods=['GET'])
def check_payment_pendiente():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id_pago FROM pagos WHERE estado='aprobado' AND dispensado=0 LIMIT 1")
    row = c.fetchone()
    conn.close()
    if row:
        return jsonify({"id_pago": row[0]})
    return jsonify({"id_pago": None})

@app.route('/marcar_dispensado', methods=['POST'])
def marcar_dispensado():
    data = request.get_json()
    id_pago = data.get("id_pago")
    if not id_pago:
        return jsonify({"error": "Falta id_pago"}), 400

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE pagos SET dispensado=1 WHERE id_pago=?", (id_pago,))
    conn.commit()
    conn.close()
    return jsonify({"status": "marcado"})

@app.route('/')
def index():
    return "Servidor Dispen-Easy funcionando."

if __name__ == '__main__':
    init_db()
  
    
   
