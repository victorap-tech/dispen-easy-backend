from flask import Flask, request, jsonify
import sqlite3
import os

app = Flask(__name__)

# Ruta a la base de datos
DB_PATH = os.getenv('DB_PATH', 'pagos.db')


@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json()
    if not data or 'id_pago' not in data:
        return jsonify({'error': 'Falta id_pago'}), 400

    id_pago = data['id_pago']

    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO pagos (id_pago, estado, dispensado) VALUES (?, ?, 0)", (id_pago, 'aprobado'))
        conn.commit()
        conn.close()
        return jsonify({'status': 'ok'}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/check_payment_pendiente', methods=['GET'])
def check_payment_pendiente():
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT id_pago FROM pagos WHERE estado = 'aprobado' AND dispensado = 0 LIMIT 1")
        result = cursor.fetchone()
        conn.close()

        if result:
            return jsonify({'pago_pendiente': True, 'id_pago': result[0]}), 200
        else:
            return jsonify({'pago_pendiente': False}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/marcar_dispensado', methods=['POST'])
def marcar_dispensado():
    data = request.get_json()
    id_pago = data.get('id_pago')
    if not id_pago:
        return jsonify({'error': 'Falta id_pago'}), 400

    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("UPDATE pagos SET dispensado = 1 WHERE id_pago = ?", (id_pago,))
        conn.commit()
        conn.close()
        return jsonify({'status': 'ok'}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500



  

  
