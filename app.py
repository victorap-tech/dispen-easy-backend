from flask import Flask, request, jsonify
import sqlite3

app = Flask(__name__)
DB_PATH = "pagos.db"

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

# Webhook para recibir pagos (desde MercadoPago o Postman)
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json()
    id_pago = data.get('id_pago')
    estado = data.get('estado')
    if id_pago and estado:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO pagos (id_pago, estado, dispensado) VALUES (?, ?, 0)", (id_pago, estado))
        conn.commit()
        conn.close()
        return jsonify({"mensaje": "Pago guardado"}), 200
    return jsonify({"error": "faltan datos"}), 400

# Endpoint que consulta si hay algún pago pendiente sin dispensar
@app.route('/check_payment_pendiente', methods=['GET'])
def check_payment_pendiente():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id_pago FROM pagos WHERE estado = 'aprobado' AND dispensado = 0 LIMIT 1")
    row = cursor.fetchone()
    conn.close()
    if row:
        return jsonify({"id_pago": row[0]})
    else:
        return jsonify({"id_pago": None})

# Marcar un pago como ya dispensado
@app.route('/marcar_dispensado', methods=['POST'])
def marcar_dispensado():
    data = request.get_json()
    id_pago = data.get('id_pago')
    if id_pago:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("UPDATE pagos SET dispensado = 1 WHERE id_pago = ?", (id_pago,))
        conn.commit()
        conn.close()
        return jsonify({"mensaje": "Marcado como dispensado"}), 200
    return jsonify({"error": "id_pago requerido"}), 400

#if __name__ == '__main__':
    #app.run(host='0.0.0.0', port=8000)
