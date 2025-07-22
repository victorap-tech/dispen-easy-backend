from flask import Flask, request, jsonify
from flask_cors import CORS
import sqlite3
import time

app = Flask(__name__)
CORS(app)

DB_PATH = "productos.db"

# ---------- INICIALIZAR TABLAS ----------
@app.route('/initdb')
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS productos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT NOT NULL,
            precio REAL NOT NULL
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS pagos (
            id_pago TEXT PRIMARY KEY,
            producto_id INTEGER,
            estado TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS heartbeat (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()
    return "Tablas inicializadas (productos, pagos, heartbeat)"

# ---------- CRUD PRODUCTOS ----------
@app.route('/productos', methods=['GET'])
def get_productos():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, nombre, precio FROM productos")
    productos = [{'id': row[0], 'nombre': row[1], 'precio': row[2]} for row in c.fetchall()]
    conn.close()
    return jsonify(productos)

@app.route('/productos', methods=['POST'])
def add_producto():
    data = request.get_json()
    nombre = data.get('nombre')
    precio = data.get('precio')
    if not nombre or precio is None:
        return jsonify({'error': 'Nombre y precio son obligatorios'}), 400
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO productos (nombre, precio) VALUES (?, ?)", (nombre, precio))
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})

@app.route('/productos/<int:producto_id>', methods=['DELETE'])
def delete_producto(producto_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM productos WHERE id = ?", (producto_id,))
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})

# ---------- WEBHOOK DE PAGOS ----------
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json()
    id_pago = data.get('id_pago')
    producto_id = data.get('producto_id')
    estado = data.get('estado', 'pendiente')
    if not id_pago or not producto_id:
        return jsonify({'error': 'Datos incompletos'}), 400
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO pagos (id_pago, producto_id, estado) VALUES (?, ?, ?)", (id_pago, producto_id, estado))
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})

# ---------- CONSULTA DE PAGOS PENDIENTES (para ESP32) ----------
@app.route('/check_payment_pendiente', methods=['GET'])
def check_payment_pendiente():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id_pago, producto_id FROM pagos WHERE estado = 'pendiente' ORDER BY timestamp LIMIT 1")
    row = c.fetchone()
    conn.close()
    if row:
        return jsonify({'id_pago': row[0], 'producto_id': row[1]})
    else:
        return jsonify({'id_pago': None})

# ---------- MARCAR PAGO COMO DISPENSADO ----------
@app.route('/marcar_dispensado', methods=['POST'])
def marcar_dispensado():
    data = request.get_json()
    id_pago = data.get('id_pago')
    if not id_pago:
        return jsonify({'error': 'id_pago requerido'}), 400
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE pagos SET estado = 'dispensado' WHERE id_pago = ?", (id_pago,))
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})

# ---------- HEARTBEAT ----------
@app.route('/heartbeat', methods=['POST'])
def heartbeat():
    data = request.get_json()
    device_id = data.get('device_id')
    if not device_id:
        return jsonify({'error': 'device_id requerido'}), 400
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO heartbeat (device_id) VALUES (?)", (device_id,))
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})

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

# ---------- ENDPOINT DE PRUEBA ----------
@app.route('/')
def home():
    return "Servidor Dispen-Easy funcionando."

if __name__ == '__main__':
    app.run(host="0.0.0.0", port=5000)
