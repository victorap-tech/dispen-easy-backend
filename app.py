from flask import Flask, request, jsonify
import sqlite3

app = Flask(__name__)
DB_PATH = 'pagos.db'

@app.route('/')
def index():
    return 'Backend Dispen-Easy funcionando correctamente.'

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json()
    if not data:
        return jsonify({'error': 'No se recibió JSON'}), 400

    id_pago = str(data.get('id'))
    estado = str(data.get('estado'))

    if not id_pago or not estado:
        return jsonify({'error': 'Faltan datos'}), 400

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR REPLACE INTO pagos (id_pago, estado, dispensado)
        VALUES (?, ?, COALESCE((SELECT dispensado FROM pagos WHERE id_pago = ?), 0))
    ''', (id_pago, estado, id_pago))
    conn.commit()
    conn.close()

    return jsonify({'status': 'ok'}), 200

@app.route('/check_payment_pendiente', methods=['GET'])
def check_payment_pendiente():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('SELECT id_pago FROM pagos WHERE estado = "approved" AND dispensado = 0 LIMIT 1')
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
    cursor.execute('UPDATE pagos SET dispensado = 1 WHERE id_pago = ?', (id_pago,))
    conn.commit()
    conn.close()

    return jsonify({'status': 'ok'}), 200

if __name__ == '__main__':
    app.run()
