from flask import Flask, request, jsonify
import sqlite3
import os

app = Flask(__name__)
DB = 'pagos.db'

# Inicializa la base si no existe
def init_db():
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS pagos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            id_pago TEXT,
            estado TEXT,
            fecha TEXT,
            dispensado INTEGER DEFAULT 0
        )
    ''')
    conn.commit()
    conn.close()

init_db()

# Webhook para recibir pagos
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json()
    if not data:
        return jsonify({"error": "No se recibió JSON"}), 400

    id_pago = data.get("id_pago")
    estado = data.get("estado")
    fecha = data.get("fecha", "")

    if not id_pago or not estado:
        return jsonify({"error": "Faltan datos obligatorios"}), 400

    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("INSERT INTO pagos (id_pago, estado, fecha) VALUES (?, ?, ?)",
              (id_pago, estado, fecha))
    conn.commit()
    conn.close()

    return jsonify({"mensaje": "Pago recibido"}), 200

# Consulta por ID (opcional)
@app.route('/check_payment', methods=['GET'])
def check_payment():
    id_pago = request.args.get('id_pago')
    if not id_pago:
        return jsonify({"error": "Falta id_pago"}), 400

    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT estado FROM pagos WHERE id_pago = ?", (id_pago,))
    row = c.fetchone()
    conn.close()

    if row:
        return jsonify({"estado": row[0]})
    else:
        return jsonify({"estado": "no_encontrado"})

# ✅ Devuelve un pago pendiente (no dispensado)
@app.route('/check_payment_pendiente', methods=['GET'])
def check_payment_pendiente():
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT id_pago FROM pagos WHERE estado = 'aprobado' AND dispensado = 0 LIMIT 1")
    row = c.fetchone()
    conn.close()

    if row:
        return jsonify({"id_pago": row[0]})
    else:
        return jsonify({"estado": "sin_pagos_pendientes"})

# ✅ Marca el pago como ya dispensado
@app.route('/marcar_dispensado', methods=['GET'])
def marcar_dispensado():
    id_pago = request.args.get('id_pago')
    if not id_pago:
        return jsonify({"error": "Falta id_pago"}), 400

    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("UPDATE pagos SET dispensado = 1 WHERE id_pago = ?", (id_pago,))
    conn.commit()
    cambios = conn.total_changes
    conn.close()

    if cambios > 0:
        return jsonify({"estado": "marcado_dispensado", "id_pago": id_pago})
    else:
        return jsonify({"estado": "id_no_encontrado"})

# ✅ Corrección para Railway: puerto dinámico
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))  # 5000 local, otro en Railway
    app.run(host='0.0.0.0', port=port)


