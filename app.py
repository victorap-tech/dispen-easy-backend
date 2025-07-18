from flask import Flask, request, jsonify
import sqlite3

app = Flask(__name__)

# Inicializa la base de datos
def init_db():
    conn = sqlite3.connect('pagos.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS pagos (
            id_pago TEXT PRIMARY KEY,
            estado TEXT,
            dispensado INTEGER DEFAULT 0
        )
    ''')
    conn.commit()
    conn.close()

# Webhook de MercadoPago
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json
    if not data:
        return 'No data', 400
    id_pago = str(data.get('id'))
    estado = data.get('estado', 'desconocido')

    conn = sqlite3.connect('pagos.db')
    cursor = conn.cursor()
    cursor.execute('INSERT OR REPLACE INTO pagos (id_pago, estado) VALUES (?, ?)', (id_pago, estado))
    conn.commit()
    conn.close()

    return 'OK', 200

# ESP32 consulta pagos pendientes
@app.route('/check_payment_pendiente', methods=['GET'])
def check_payment_pendiente():
    conn = sqlite3.connect('pagos.db')
    cursor = conn.cursor()
    cursor.execute('SELECT id_pago FROM pagos WHERE estado = "approved" AND dispensado = 0 LIMIT 1')
    row = cursor.fetchone()
    conn.close()

    if row:
        return jsonify({'id_pago': row[0]})
    else:
        return jsonify({'id_pago': None})

# Marcar pago como ya dispensado
@app.route('/marcar_dispensado', methods=['POST'])
def marcar_dispensado():
    data = request.json
    id_pago = data.get('id_pago')
    if not id_pago:
        return 'Falta id_pago', 400

    conn = sqlite3.connect('pagos.db')
    cursor = conn.cursor()
    cursor.execute('UPDATE pagos SET dispensado = 1 WHERE id_pago = ?', (id_pago,))
    conn.commit()
    conn.close()
    return 'OK', 200

if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=8000)
  
    
   
