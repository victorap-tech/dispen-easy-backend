from flask import Flask, request, jsonify
import sqlite3
import os

app = Flask(__name__)

# Crear base de datos si no existe
def init_db():
    conn = sqlite3.connect('pagos.db')
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS pagos (
                        id_pago TEXT PRIMARY KEY,
                        status TEXT
                      )''')
    conn.commit()
    conn.close()

init_db()

@app.route('/')
def home():
    return 'Servidor funcionando correctamente'

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json()
    id_pago = str(data.get('data', {}).get('id', ''))
    if not id_pago:
        return jsonify({'message': 'ID no válido'}), 400

    conn = sqlite3.connect('pagos.db')
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO pagos (id_pago, status) VALUES (?, ?)", (id_pago, 'aprobado'))
    conn.commit()
    conn.close()
    return jsonify({'message': 'Pago recibido'}), 200

@app.route('/check_payment', methods=['GET'])
def check_payment():
    id_pago = request.args.get('id_pago')
    if not id_pago:
        return jsonify({'status': 'error', 'message': 'Falta id_pago'}), 400

    conn = sqlite3.connect('pagos.db')
    cursor = conn.cursor()
    cursor.execute("SELECT status FROM pagos WHERE id_pago=?", (id_pago,))
    row = cursor.fetchone()
    conn.close()

    if row:
        return jsonify({'estado': row[0], 'status': 'ok'})
    else:
        return jsonify({'status': 'error', 'message': 'Pago no encontrado'}), 404

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
