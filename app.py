from flask import Flask, request, jsonify
import sqlite3
from datetime import datetime

app = Flask(__name__)
DB_NAME = 'pagos.db'

crear_tabla_si_no_existe()

# 🧱 Función para crear tabla si no existe
def crear_tabla_si_no_existe():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS pagos (
            id_pago TEXT PRIMARY KEY,
            estado TEXT,
            fecha TEXT
        )
    ''')
    conn.commit()
    conn.close()

# ✅ Ejecutar al iniciar
crear_tabla_si_no_existe()

# 🔁 Webhook para recibir notificaciones de MercadoPago
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json()
    if not data:
        return jsonify({'message': 'Faltan datos', 'status': 'error'}), 400

    try:
        id_pago = str(data['data']['id'])
        estado = data.get('type', 'desconocido')
        fecha = datetime.now().isoformat()

        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO pagos (id_pago, estado, fecha)
            VALUES (?, ?, ?)
        ''', (id_pago, estado, fecha))
        conn.commit()
        conn.close()

        return jsonify({'message': 'Recibido', 'status': 'ok'}), 200

    except Exception as e:
        return jsonify({'message': str(e), 'status': 'error'}), 500

# 🔍 Endpoint para consultar pagos
@app.route('/check_payment', methods=['GET'])
def check_payment():
    id_pago = request.args.get('id_pago')
    if not id_pago:
        return jsonify({'status': 'error', 'message': 'Falta el parámetro id_pago'}), 400

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('SELECT estado FROM pagos WHERE id_pago = ?', (id_pago,))
    row = cursor.fetchone()
    conn.close()

    if row and row[0] == 'payment':
        return jsonify({'estado': 'aprobado', 'status': 'ok'}), 200
    else:
        return jsonify({'estado': 'pendiente', 'status': 'error'}), 404

#if __name__ == '__main__':
    #app.run(host='0.0.0.0', port=5000)


