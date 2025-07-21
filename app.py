from flask import Flask, request, jsonify, send_file
import sqlite3

app = Flask(__name__)
DB_PATH = "pagos.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # Tabla de pagos
    c.execute("""
        CREATE TABLE IF NOT EXISTS pagos (
            id_pago TEXT PRIMARY KEY,
            estado TEXT,
            dispensado INTEGER DEFAULT 0
        )
    """)
    # Tabla de fallas
    c.execute("""
        CREATE TABLE IF NOT EXISTS fallas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            descripcion TEXT,
            fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json()
    id_pago = data.get('id_pago')
    estado = data.get('estado')
    if id_pago and estado:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(
            "INSERT OR REPLACE INTO pagos (id_pago, estado, dispensado) VALUES (?, ?, COALESCE((SELECT dispensado FROM pagos WHERE id_pago=?), 0))",
            (id_pago, estado, id_pago)
        )
        conn.commit()
        conn.close()
        return jsonify({'status': 'ok'}), 200
    else:
        return jsonify({'error': 'Datos incompletos'}), 400

@app.route('/check_payment_pendiente', methods=['GET'])
def check_payment_pendiente():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id_pago FROM pagos WHERE estado='aprobado' AND dispensado=0 LIMIT 1")
    row = c.fetchone()
    conn.close()
    if row:
        return jsonify({'id_pago': row[0]})
    else:
        return jsonify({'id_pago': None})

@app.route('/marcar_dispensado', methods=['POST'])
def marcar_dispensado():
    data = request.get_json()
    id_pago = data.get('id_pago')
    if not id_pago:
        return jsonify({'error': 'Falta id_pago'}), 400
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE pagos SET dispensado=1 WHERE id_pago=?", (id_pago,))
    conn.commit()
    conn.close()
    return jsonify({'status': 'marcado'})

@app.route('/ver_pagos', methods=['GET'])
def ver_pagos():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM pagos")
    rows = c.fetchall()
    conn.close()
    pagos = [{'id_pago': row[0], 'estado': row[1], 'dispensado': row[2]} for row in rows]
    return jsonify(pagos)

@app.route('/borrar_pagos', methods=['POST'])
def borrar_pagos():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM pagos")
    conn.commit()
    conn.close()
    return jsonify({'status': 'borrados'})

@app.route('/descargar_db', methods=['GET'])
def descargar_db():
    return send_file(DB_PATH, as_attachment=True)

# --- NUEVOS ENDPOINTS DE FALLAS ---

@app.route('/registrar_falla', methods=['POST'])
def registrar_falla():
    data = request.get_json()
    descripcion = data.get('descripcion', 'Sin descripción')
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO fallas (descripcion) VALUES (?)", (descripcion,))
    conn.commit()
    conn.close()
    return jsonify({'status': 'falla registrada'})

@app.route('/ver_fallas', methods=['GET'])
def ver_fallas():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, descripcion, fecha FROM fallas ORDER BY fecha DESC")
    rows = c.fetchall()
    conn.close()
    fallas = [{'id': row[0], 'descripcion': row[1], 'fecha': row[2]} for row in rows]
    return jsonify(fallas)

@app.route('/')
def index():
    return "Servidor Dispen-Easy funcionando."

# Inicializar base de datos
init_db()

# Para Railway no incluyas app.run()
# Si vas a correr local, descomentá esto:
# if __name__ == "__main__":
#     app.run(host="0.0.0.0", port=5000)
