from flask import Flask, request, jsonify
import sqlite3
import os

app = Flask(__name__)
DB_PATH = os.path.join(os.path.dirname(__file__), 'pagos.db')


@app.route('/')
def index():
    return 'Servidor Dispen-Easy funcionando correctamente', 200


@app.route('/check_payment_pendiente', methods=['GET'])
def check_payment_pendiente():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id_pago FROM pagos WHERE estado = 'aprobado' AND dispensado = 0 LIMIT 1")
    result = cursor.fetchone()
    conn.close()

    if result:
        return jsonify({'id_pago': result[0]}), 200
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
    cursor.execute("UPDATE pagos SET dispensado = 1 WHERE id_pago = ?", (id_pago,))
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'}), 200
