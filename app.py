from flask import Flask, request, jsonify, send_file
import sqlite3

app = Flask(__name__)
DB_PATH = "pagos.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS pagos (
            id_pago TEXT PRIMARY KEY,
            estado TEXT,
            dispensado INTEGER DEFAULT 0
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

@app.route('/')
def index():
    return "Servidor Dispen-Easy funcionando."

# Inicializa la base de datos si no existe
init_db()

# Para Railway no pongas app.run()
# Si lo ejecutás local, descomentá estas líneas:
# if __name__ == "__main__":
#     app.run(host="0.0.0.0", port=5000)
