from flask import Flask, request, jsonify
import sqlite3

app = Flask(__name__)

# Inicializar la base de datos (si no existe)
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

# Ruta para recibir pagos desde el webhook de MercadoPago
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json()
    if not data or 'id_pago' not in data or 'estado' not in data:
        return jsonify({"error": "Datos inválidos"}), 400

    id_pago = str(data['id_pago'])
    estado = data['estado']

    conn = sqlite3.connect('pagos.db')
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO pagos (id_pago, estado, dispensado) VALUES (?, ?, 0)", (id_pago, estado))
    conn.commit()
    conn.close()
    return jsonify({"mensaje": "Pago recibido"}), 200

# Ruta para consultar si hay algún pago aprobado y no dispensado
@app.route('/check_payment_pendiente')
def check_payment_pendiente():
    conn = sqlite3.connect('pagos.db')
    cursor = conn.cursor()
    cursor.execute("SELECT id_pago, estado FROM pagos WHERE estado = 'aprobado' AND dispensado = 0 LIMIT 1")
    row = cursor.fetchone()
    conn.close()
    if row:
        return jsonify({"id_pago": row[0], "estado": row[1]})
    else:
        return jsonify({})

# Ruta para marcar un pago como ya dispensado
@app.route('/marcar_dispensado', methods=['POST'])
def marcar_dispensado():
    data = request.get_json()
    if not data or 'id_pago' not in data:
        return jsonify({"error": "Falta el id_pago"}), 400

    id_pago = str(data['id_pago'])

    conn = sqlite3.connect('pagos.db')
    cursor = conn.cursor()
    cursor.execute("UPDATE pagos SET dispensado = 1 WHERE id_pago = ?", (id_pago,))
    conn.commit()
    conn.close()
    return jsonify({"mensaje": "Pago marcado como dispensado"}), 200

if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=8000)



  
