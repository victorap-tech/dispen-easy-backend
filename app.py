from flask import Flask, request, jsonify
from flask_cors import CORS
import sqlite3
import time

app = Flask(__name__)
CORS(app)

DB_PATH = "productos.db"

# ---- Inicialización de base de datos ----
@app.route('/initdb')
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS productos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT NOT NULL,
            precio REAL NOT NULL,
            link_pago TEXT
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS pagos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            producto_id INTEGER,
            monto REAL,
            estado TEXT,
            mp_id TEXT,
            raw_data TEXT,
            timestamp INTEGER DEFAULT (strftime('%s','now'))
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS heartbeat (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT,
            timestamp INTEGER DEFAULT (strftime('%s','now'))
        )
    ''')
    conn.commit()
    conn.close()
    return "Tablas inicializadas (productos, pagos, heartbeat)"

# ---- CRUD Productos ----
@app.route('/productos', methods=['GET'])
def get_productos():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, nombre, precio, link_pago FROM productos")
    productos = [
        {'id': row[0], 'nombre': row[1], 'precio': row[2], 'link_pago': row[3]}
        for row in c.fetchall()
    ]
    conn.close()
    return jsonify(productos)

@app.route('/productos', methods=['POST'])
def add_producto():
    data = request.json
    nombre = data.get('nombre')
    precio = data.get('precio')
    link_pago = data.get('link_pago', None)
    if not nombre or not precio:
        return "Faltan datos", 400
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO productos (nombre, precio, link_pago) VALUES (?, ?, ?)", (nombre, precio, link_pago))
    conn.commit()
    conn.close()
    return "Producto agregado", 200

@app.route('/productos/<int:prod_id>', methods=['DELETE'])
def delete_producto(prod_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM productos WHERE id = ?", (prod_id,))
    conn.commit()
    conn.close()
    return "Producto eliminado", 200

# ---- PAGOS ----
@app.route('/pagos', methods=['GET'])
def get_pagos():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, producto_id, monto, estado, mp_id, timestamp FROM pagos ORDER BY timestamp DESC LIMIT 20")
    pagos = [
        {'id': row[0], 'producto_id': row[1], 'monto': row[2], 'estado': row[3], 'mp_id': row[4], 'timestamp': row[5]}
        for row in c.fetchall()
    ]
    conn.close()
    return jsonify(pagos)

@app.route('/pagos', methods=['POST'])
def add_pago():
    data = request.json
    producto_id = data.get('producto_id')
    monto = data.get('monto')
    estado = data.get('estado', 'pendiente')
    mp_id = data.get('mp_id', '')
    raw_data = str(data)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO pagos (producto_id, monto, estado, mp_id, raw_data) VALUES (?, ?, ?, ?, ?)",
              (producto_id, monto, estado, mp_id, raw_data))
    conn.commit()
    conn.close()
    return "Pago registrado", 200

# ---- WEBHOOK MERCADOPAGO ----
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json
    # Aquí podés customizar el análisis del webhook según la info que envía MercadoPago
    mp_id = None
    monto = None
    estado = None
    producto_id = None
    if data:
        # Ejemplo: extraer el ID y estado de un pago (modifica según tu payload real)
        mp_id = data.get('data', {}).get('id')
        estado = data.get('type') or data.get('action') or "desconocido"
        monto = data.get('monto', 0)
    raw_data = str(data)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO pagos (producto_id, monto, estado, mp_id, raw_data) VALUES (?, ?, ?, ?, ?)",
              (producto_id, monto, estado, mp_id, raw_data))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"}), 200

# ---- HEARTBEAT (dispositivos conectados, opcional) ----
@app.route('/heartbeat', methods=['POST'])
def heartbeat():
    data = request.json
    device_id = data.get('device_id')
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO heartbeat (device_id) VALUES (?)", (device_id,))
    conn.commit()
    conn.close()
    return "OK", 200

@app.route('/heartbeat/<device_id>', methods=['GET'])
def ver_heartbeat(device_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT timestamp FROM heartbeat WHERE device_id=? ORDER BY timestamp DESC LIMIT 1", (device_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return jsonify({'device_id': device_id, 'ultimo_heartbeat': row[0]})
    else:
        return jsonify({'error': 'No hay heartbeat registrado'}), 404

# ---- Ruta raíz para ver que está online ----
@app.route('/')
def index():
    return "Servidor Dispen-Easy funcionando."

if __name__ == '__main__':
    app.run(debug=True, host="0.0.0.0")
