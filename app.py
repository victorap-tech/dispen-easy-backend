from flask import Flask, request, jsonify
from flask_cors import CORS
import sqlite3
import time

app = Flask(__name__)
CORS(app)

DB_PATH = "productos.db"

# ------- INICIALIZAR TABLAS ---------
@app.route('/initdb')
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # Tabla de productos
    c.execute('''
        CREATE TABLE IF NOT EXISTS productos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT NOT NULL,
            precio REAL NOT NULL,
            link_pago TEXT
        )
    ''')
    # Tabla de pagos (para webhook)
    c.execute('''
        CREATE TABLE IF NOT EXISTS pagos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            producto_id INTEGER,
            estado TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # Heartbeat (opcional)
    c.execute('''
        CREATE TABLE IF NOT EXISTS heartbeat (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()
    return "Tablas inicializadas (productos, pagos, heartbeat)"

# ------- PRODUCTOS (CRUD) -----------
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
    c.execute("INSERT INTO productos (nombre, precio, link_pago) VALUES (?, ?, ?)",
              (nombre, precio, link_pago))
    conn.commit()
    conn.close()
    return "Producto agregado", 201

@app.route('/productos/<int:prod_id>', methods=['DELETE'])
def delete_producto(prod_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM productos WHERE id=?", (prod_id,))
    conn.commit()
    conn.close()
    return "Producto borrado", 200

# ------- WEBHOOK DE PAGOS -----------
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json
    producto_id = data.get('producto_id')
    estado = data.get('estado', 'pendiente')
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO pagos (producto_id, estado) VALUES (?, ?)", (producto_id, estado))
    conn.commit()
    conn.close()
    return "Webhook recibido", 200

# ------- HEARTBEAT (opcional) -------
@app.route('/heartbeat', methods=['POST'])
def heartbeat():
    data = request.json
    device_id = data.get('device_id')
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO heartbeat (device_id) VALUES (?)", (device_id,))
    conn.commit()
    conn.close()
    return "Heartbeat ok", 200

@app.route('/')
def index():
    return "Servidor Dispen-Easy funcionando."

# --------- PARA RAILWAY -------------
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000)
