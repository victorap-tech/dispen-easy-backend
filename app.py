from flask import Flask, request, jsonify
from flask_cors import CORS
import sqlite3
import time

app = Flask(__name__)
CORS(app)

DB_PATH = "productos.db"

# --------- CORS FIX GLOBAL PARA RAILWAY ----------
@app.after_request
def after_request(response):
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
    response.headers.add('Access-Control-Allow-Methods', 'GET,PUT,POST,DELETE,OPTIONS')
    return response

# ---------- INICIALIZAR TABLAS ----------
@app.route('/initdb')
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # Productos
    c.execute('''
        CREATE TABLE IF NOT EXISTS productos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT NOT NULL,
            precio REAL NOT NULL,
            link_pago TEXT
        )
    ''')
    # Pagos
    c.execute('''
        CREATE TABLE IF NOT EXISTS pagos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            estado TEXT,
            producto_id INTEGER,
            fecha TEXT,
            monto REAL,
            mp_payment_id TEXT,
            mp_topic TEXT
        )
    ''')
    # Heartbeat
    c.execute('''
        CREATE TABLE IF NOT EXISTS heartbeat (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT,
            timestamp TEXT
        )
    ''')
    conn.commit()
    conn.close()
    return "Tablas inicializadas (productos, pagos, heartbeat)"

# ----------- PRODUCTOS (CRUD) -----------
@app.route('/productos', methods=['GET', 'POST', 'OPTIONS'])
def productos():
    if request.method == 'OPTIONS':
        return '', 200

    if request.method == 'GET':
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT id, nombre, precio, link_pago FROM productos")
        productos = [
            {'id': row[0], 'nombre': row[1], 'precio': row[2], 'link_pago': row[3]}
            for row in c.fetchall()
        ]
        conn.close()
        return jsonify(productos)

    if request.method == 'POST':
        data = request.json
        nombre = data.get('nombre')
        precio = data.get('precio')
        link_pago = data.get('link_pago', None)
        if not nombre or not precio:
            return "Faltan datos", 400
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(
            "INSERT INTO productos (nombre, precio, link_pago) VALUES (?, ?, ?)",
            (nombre, precio, link_pago)
        )
        conn.commit()
        conn.close()
        return "Producto agregado", 201

# ---------- BORRAR PRODUCTO ----------
@app.route('/productos/<int:id>', methods=['DELETE'])
def delete_producto(id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM productos WHERE id=?", (id,))
    conn.commit()
    conn.close()
    return "Producto borrado", 200

# ---------- WEBHOOK DE MERCADOPAGO ----------
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json
    producto_id = data.get('producto_id', None)
    estado = data.get('estado', None)
    monto = data.get('monto', None)
    mp_payment_id = data.get('mp_payment_id', None)
    mp_topic = data.get('mp_topic', None)
    fecha = time.strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        INSERT INTO pagos (estado, producto_id, fecha, monto, mp_payment_id, mp_topic)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (estado, producto_id, fecha, monto, mp_payment_id, mp_topic))
    conn.commit()
    conn.close()
    return "Webhook recibido", 200

# ---------- CONSULTAR PAGOS ----------
@app.route('/pagos', methods=['GET'])
def get_pagos():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, estado, producto_id, fecha, monto, mp_payment_id, mp_topic FROM pagos")
    pagos = [
        {
            'id': row[0],
            'estado': row[1],
            'producto_id': row[2],
            'fecha': row[3],
            'monto': row[4],
            'mp_payment_id': row[5],
            'mp_topic': row[6]
        }
        for row in c.fetchall()
    ]
    conn.close()
    return jsonify(pagos)

# ---------- HEARTBEAT ----------
@app.route('/heartbeat', methods=['POST'])
def heartbeat():
    data = request.json
    device_id = data.get('device_id', None)
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO heartbeat (device_id, timestamp) VALUES (?, ?)", (device_id, timestamp))
    conn.commit()
    conn.close()
    return "Heartbeat registrado", 200

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

# ----------- HOME -------------
@app.route('/')
def home():
    return "Servidor Dispen-Easy funcionando."

# ----------- PARA DESARROLLO LOCAL -------------
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
