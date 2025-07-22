from flask import Flask, request, jsonify, send_file
import sqlite3

app = Flask(__name__)
DB_PATH = "pagos.db"

# ----------------- Inicialización de la base ----------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # PAGOS
    c.execute("""
        CREATE TABLE IF NOT EXISTS pagos (
            id_pago TEXT PRIMARY KEY,
            estado TEXT,
            dispensado INTEGER DEFAULT 0,
            producto TEXT,
            fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # FALLAS
    c.execute("""
        CREATE TABLE IF NOT EXISTS fallas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            descripcion TEXT,
            fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # HEARTBEAT
    c.execute("""
        CREATE TABLE IF NOT EXISTS heartbeat (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # PRODUCTOS
    c.execute("""
        CREATE TABLE IF NOT EXISTS productos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT NOT NULL,
            precio REAL NOT NULL
        )
    """)
    conn.commit()
    conn.close()

# ----------------- WEBHOOK Y PAGOS --------------------------
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json()
    id_pago = data.get('id_pago')
    estado = data.get('estado')
    producto = data.get('producto', data.get('external_reference', 'desconocido'))
    if id_pago and estado:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(
            "INSERT OR REPLACE INTO pagos (id_pago, estado, dispensado, producto, fecha) VALUES (?, ?, COALESCE((SELECT dispensado FROM pagos WHERE id_pago=?), 0), ?, COALESCE((SELECT fecha FROM pagos WHERE id_pago=?), CURRENT_TIMESTAMP))",
            (id_pago, estado, id_pago, producto, id_pago)
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
    c.execute("SELECT id_pago, producto FROM pagos WHERE estado='aprobado' AND dispensado=0 LIMIT 1")
    row = c.fetchone()
    conn.close()
    if row:
        return jsonify({'id_pago': row[0], 'producto': row[1]})
    else:
        return jsonify({'id_pago': None, 'producto': None})

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
    c.execute("SELECT id_pago, estado, dispensado, producto, fecha FROM pagos")
    rows = c.fetchall()
    conn.close()
    pagos = [{'id_pago': row[0], 'estado': row[1], 'dispensado': row[2], 'producto': row[3], 'fecha': row[4]} for row in rows]
    return jsonify(pagos)

@app.route('/buscar_pago/<id_pago>', methods=['GET'])
def buscar_pago(id_pago):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id_pago, estado, dispensado, producto, fecha FROM pagos WHERE id_pago=?", (id_pago,))
    row = c.fetchone()
    conn.close()
    if row:
        return jsonify({'id_pago': row[0], 'estado': row[1], 'dispensado': row[2], 'producto': row[3], 'fecha': row[4]})
    else:
        return jsonify({'error': 'Pago no encontrado'}), 404

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

# ----------------- FALLAS --------------------------
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

# ----------------- HEARTBEAT --------------------------
@app.route('/heartbeat', methods=['POST'])
def heartbeat():
    data = request.get_json()
    device_id = data.get('device_id', 'esp32')
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO heartbeat (device_id, timestamp) VALUES (?, datetime('now'))", (device_id,))
    conn.commit()
    conn.close()
    return jsonify({'status': 'heartbeat recibido'})

@app.route('/ver_heartbeat/<device_id>', methods=['GET'])
def ver_heartbeat(device_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT timestamp FROM heartbeat WHERE device_id=? ORDER BY timestamp DESC LIMIT 1", (device_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return jsonify({'device_id': device_id, 'ultimo_heartbeat': row[0]})
    else:
        return jsonify({'error': 'No hay heartbeat registrado para este dispositivo'})

# ----------------- PRODUCTOS (CRUD) --------------------------
@app.route('/productos', methods=['GET'])
def get_productos():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, nombre, precio FROM productos")
    productos = [{"id": row[0], "nombre": row[1], "precio": row[2]} for row in c.fetchall()]
    conn.close()
    return jsonify(productos)

@app.route('/productos', methods=['POST'])
def add_producto():
    data = request.get_json()
    nombre = data.get("nombre")
    precio = data.get("precio")
    if not nombre or precio is None:
        return jsonify({"error": "Faltan datos"}), 400
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO productos (nombre, precio) VALUES (?, ?)", (nombre, precio))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})

@app.route('/productos/<int:prod_id>', methods=['PUT'])
def update_producto(prod_id):
    data = request.get_json()
    nombre = data.get("nombre")
    precio = data.get("precio")
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE productos SET nombre=?, precio=? WHERE id=?", (nombre, precio, prod_id))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})

@app.route('/productos/<int:prod_id>', methods=['DELETE'])
def delete_producto(prod_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM productos WHERE id=?", (prod_id,))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})

# ----------------- RAÍZ --------------------------
@app.route('/')
def index():
    return "Servidor Dispen-Easy funcionando."

# Inicializar base si no existe
init_db()
