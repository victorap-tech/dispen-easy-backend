from flask import Flask, request, jsonify import sqlite3 import os

app = Flask(name)

Crear base de datos si no existe

def init_db(): conn = sqlite3.connect('pagos.db') cursor = conn.cursor() cursor.execute('''CREATE TABLE IF NOT EXISTS pagos ( id_pago TEXT PRIMARY KEY, status TEXT, raw TEXT )''') conn.commit() conn.close()

init_db()

Endpoint de prueba

@app.route('/') def home(): return 'Backend de Dispen-Easy funcionando correctamente.'

Endpoint para verificar estado de pago

@app.route('/check_payment', methods=['GET']) def check_payment(): id_pago = request.args.get('id_pago') if not id_pago: return jsonify({'status': 'error', 'message': 'Falta id_pago'})

conn = sqlite3.connect('pagos.db')
cursor = conn.cursor()
cursor.execute('SELECT status FROM pagos WHERE id_pago = ?', (id_pago,))
row = cursor.fetchone()
conn.close()

if row:
    return jsonify({'estado': row[0], 'status': 'ok'})
else:
    return jsonify({'message': 'pago no encontrado', 'status': 'error'})

Endpoint para recibir notificaciones de MercadoPago

@app.route('/webhook', methods=['POST']) def webhook(): data = request.get_json() if not data: return 'No se recibió JSON', 400

try:
    id_pago = data['data']['id']  # ID de pago
    action = data.get('action', '')

    # Guardamos como 'pendiente' o 'recibido', según necesites
    conn = sqlite3.connect('pagos.db')
    cursor = conn.cursor()
    cursor.execute('''INSERT OR REPLACE INTO pagos (id_pago, status, raw)
                      VALUES (?, ?, ?)''', (id_pago, action, str(data)))
    conn.commit()
    conn.close()

    return 'OK', 200
except Exception as e:
    return str(e), 500

if name == 'main': port = int(os.environ.get('PORT', 5000)) app.run(host='0.0.0.0', port=port)
