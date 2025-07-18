from flask import Flask, request, jsonify
import sqlite3
import os

app = Flask(__name__)
DB_PATH = 'pagos.db'

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS pagos (
            id_pago TEXT PRIMARY KEY,
            estado TEXT,
            dispensado INTEGER
        )
    ''')
    conn.commit()
    conn.close()

init_db()

@app.route('/')
def home():
    return 'Dispen-Easy backend OK'

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json()
    id_pago = data.get('id')
    estado = data.get('estado')
    if not id_pago or not estado:
        return jsonify({'error': 'Faltan datos'}), 400

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR REPLACE INTO pagos (id_pago, estado, dispensado)
        VALUES (?, ?, 0)
    ''', (id_pago, estado))
    conn.commit()
    conn.close()
    return jsonify({'status': 'guardado'}), 200

@app.route('/check_payment_pendiente', methods=['GET'])
def check_payment_pendiente():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id_pago FROM pagos
        WHERE estado = 'aprobado' AND dispensado = 0
        LIMIT 1
    ''')
    row = cursor.fetchone()
    conn.close()
    if row:
        return jsonify({'id_pago': row[0]})
    else:
        return jsonify({'id_pago': None})

@app.route('/marcar_dispensado', methods=['POST'])
def marcar_dispensado():
    data = request.get_json()
    id_pago = data.get('id_pago')
    if not id_pago:
        return jsonify({'error': 'Falta id_pago'}), 400

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE pagos SET dispensado = 1 WHERE id_pago = ?
    ''', (id_pago,))
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'}), 200
  

if __name__ == "__main__":
    app.run()

  
