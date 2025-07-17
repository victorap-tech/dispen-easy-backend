from flask import Flask, request, jsonify
import sqlite3

app = Flask(__name__)
DB_PATH = '/tmp/pagos.db'

# Inicializar base de datos (si no existe)
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

# Webhook para recibir pagos de MercadoPago
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json()

    if not data:
        return jsonify({'error': 'No hay datos'}), 400

    id_pago = str(data.get('id_pago'))
    estado = data.get('estado')

    if not id_pago or not estado:
        return jsonify({'error': 'Faltan campos obligatorios'}), 400

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR REPLACE INTO pagos (id_pago, estado, dispensado)
        VALUES (?, ?, 0)
    ''', (id_pago, estado))
    conn.commit()
    conn.close()

    return jsonify({'status': 'guardado'}), 200

# Endpoint para verificar si hay pagos aprobados y no dispensados
@app.route('/check_payment_pendiente', methods=['GET'])
def check_payment_pendiente():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id_pago, estado FROM pagos
        WHERE estado = 'aprobado' AND dispensado = 0
        LIMIT 1
    ''')
    row = cursor.fetchone()
    conn.close()

    if row:
        return jsonify({'id_pago': row[0], 'estado': row[1]})
    else:
        return jsonify({'id_pago': None, 'estado': None})

# Endpoint para marcar un pago como ya dispensado
@app.route('/marcar_dispensado', methods=['POST'])
def marcar_dispensado():
    data = request.get_json()
    id_pago = data.get('id_pago')

    if not id_pago:
        return jsonify({'error': 'Falta el id_pago'}), 400

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        UPDATE pagos SET dispensado = 1 WHERE id_pago = ?
    ''', (id_pago,))
    conn.commit()
    conn.close()

    return jsonify({'status': 'ok'})

# Ruta de prueba
@app.route('/')
def home():
    return 'Servidor Dispen-Easy funcionando'

if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=8000)
