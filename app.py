from flask import Flask, request, jsonify
import sqlite3
import os

app = Flask(__name__)
DB_PATH = os.path.join(os.path.dirname(__file__), 'pagos.db')

@app.route('/')
def index():
    return 'Dispen-Easy backend funcionando'

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json()
    if not data or 'id_pago' not in data:
        return jsonify({'error': 'Falta id_pago'}), 400

    id_pago = data['id_pago']
    estado = data.get('estado', 'pendiente')

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO pagos (id_pago, estado, dispensado)
        VALUES (?, ?, 0)
    ''', (id_pago, estado))
    conn.commit()
    conn.close()

    return jsonify({'status': 'ok'}), 200

@app.route('/check_payment', methods=['GET'])
def check_payment():
    id_pago = request.args.get('id_pago')
    if not id_pago:
        return jsonify({'error': 'Falta id_pago'}), 400

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT estado, dispensado FROM pagos WHERE id_pago = ?', (id_pago,))
    row = cursor.fetchone()
    conn.close()

    if row:
        return jsonify({'estado': row[0], 'dispensado': bool(row[1])}), 200
    else:
        return jsonify({'error': 'Pago no encontrado'}), 404

@app.route('/check_payment_pendiente', methods=['GET'])
def check_payment_pendiente():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id_pago FROM pagos 
        WHERE estado = 'aprobado' AND dispensado = 0
        ORDER BY rowid ASC LIMIT 1
    ''')
    row = cursor.fetchone()
    conn.close()

    if row:
        return jsonify({'id_pago': row[0]}), 200
    else:
        return jsonify({'id_pago': None}), 200

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

# No hace falta usar app.run() si usás gunicorn en Railway
      
