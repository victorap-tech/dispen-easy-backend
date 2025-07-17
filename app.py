from flask import Flask, request, jsonify
import sqlite3
import os

DB_PATH = 'pagos.db'

app = Flask(__name__)

# Inicializa la base de datos si no existe
def init_db():
    if not os.path.exists(DB_PATH):
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE pagos (
                id_pago TEXT PRIMARY KEY,
                estado TEXT,
                dispensado INTEGER DEFAULT 0
            )
        ''')
        conn.commit()
        conn.close()

# Endpoint para recibir pagos desde MercadoPago (webhook)
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
    else:
        return jsonify({"error": "Faltan datos"}), 400

# Endpoint para que ESP32 consulte si hay pagos pendientes
@app.route('/check_payment_pendiente', methods=['GET'])
def check_payment_pendiente():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id_pago FROM pagos WHERE estado = 'approved' AND dispensado = 0 LIMIT 1")
    fila = cursor.fetchone()
    conn.close()

    if fila:
        return jsonify({"id_pago": fila[0]}), 200
    else:
        return jsonify({"id_pago": None}), 200

# Endpoint para marcar un pago como ya dispensado
@app.route('/marcar_dispensado', methods=['POST'])
def marcar_dispensado():
    data = request.get_json()
    id_pago = data.get("id_pago")

    if id_pago:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("UPDATE pagos SET dispensado = 1 WHERE id_pago = ?", (id_pago,))
        conn.commit()
        conn.close()
        return jsonify({"mensaje": "Pago marcado como dispensado"}), 200
    else:
        return jsonify({"error": "ID de pago no proporcionado"}), 400

# Inicia la base de datos
init_db()

# Ejecutar usando waitress para producción
if __name__ == "__main__":
    from waitress import serve
    port = int(os.environ.get("PORT", 5000))
    serve(app, host="0.0.0.0", port=port)
