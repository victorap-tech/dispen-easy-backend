from flask import Flask, request, jsonify
from flask_cors import CORS
import sqlite3
import time

app = Flask(__name__)
CORS(app)

DB_PATH = "productos.db"

# ---------------- INICIALIZAR TABLAS ----------------
@app.route('/initdb')
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # Tabla productos
    c.execute('''
        CREATE TABLE IF NOT EXISTS productos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT NOT NULL,
            precio REAL NOT NULL,
            link_pago TEXT
        )
    ''')
    # Tabla pagos
    c.execute('''
        CREATE TABLE IF NOT EXISTS pagos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            producto_id INTEGER,
            estado TEXT,
            fecha TEXT,
            id_pago_mercadopago TEXT,
            FOREIGN KEY (producto_id) REFERENCES productos(id)
        )
    ''')
    conn.commit()
    conn.close()
    return "Tablas inicializadas"

# ---------------- PRODUCTOS ----------------
@app.route('/productos', methods=['GET', 'POST'])
def productos():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if request.method == 'POST':
        data = request.json
        nombre = data.get('nombre')
        precio = data.get('precio')
        link_pago = data.get('link_pago')
        c.execute("INSERT INTO productos (nombre, precio, link_pago) VALUES (?, ?, ?)", (nombre, precio, link_pago))
        conn.commit()
    c.execute("SELECT id, nombre, precio, link_pago FROM productos")
    productos = [{'id': row[0], 'nombre': row[1], 'precio': row[2], 'link_pago': row[3]} for row in c.fetchall()]
    conn.close()
    return jsonify(productos)

# ---------------- BORRAR PRODUCTO ----------------
@app.route('/productos/<int:id>', methods=['DELETE'])
def borrar_producto(id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM productos WHERE id=?", (id,))
    conn.commit()
    conn.close()
    return "Producto eliminado", 200

# ---------------- WEBHOOK DE MERCADOPAGO ----------------
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json
    # Simulación: suponemos que recibimos {"id_pago_mercadopago": "...", "link_pago": "..."}
    id_pago_mercadopago = data.get('id_pago_mercadopago')
    link_pago = data.get('link_pago')
    if not (id_pago_mercadopago and link_pago):
        return "Datos insuficientes", 400
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # Buscar el producto según el link de pago fijo
    c.execute("SELECT id FROM productos WHERE link_pago = ?", (link_pago,))
    row = c.fetchone()
    if not row:
        conn.close()
        return "Producto no encontrado", 404
    producto_id = row[0]
    # Registrar el pago como pendiente
    c.execute(
        "INSERT INTO pagos (producto_id, estado, fecha, id_pago_mercadopago) VALUES (?, ?, ?, ?)",
        (producto_id, "pendiente", time.strftime("%Y-%m-%d %H:%M:%S"), id_pago_mercadopago)
    )
    conn.commit()
    conn.close()
    return "Pago registrado", 200
               # Endpoint para ver todos los pagos registrados
@app.route('/pagos', methods=['GET'])
def ver_pagos():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, producto_id, estado, fecha, id_pago_mercadopago FROM pagos")
    pagos = [
        {
            'id': row[0],
            'producto_id': row[1],
            'estado': row[2],
            'fecha': row[3],
            'id_pago_mercadopago': row[4]
        }
        for row in c.fetchall()
    ]
    conn.close()
    return jsonify(pagos)
# ---------------- CONSULTAR PAGOS PENDIENTES POR PRODUCTO ----------------
@app.route('/pago_pendiente/<int:producto_id>', methods=['GET'])
def pago_pendiente(producto_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id FROM pagos WHERE producto_id = ? AND estado = 'pendiente' ORDER BY fecha LIMIT 1", (producto_id,))
    row = c.fetchone()
    conn.close()
    if row:
        return jsonify({"pago_id": row[0], "pendiente": True})
    else:
        return jsonify({"pendiente": False})

# ---------------- MARCAR PAGO COMO DISPENSADO ----------------
@app.route('/marcar_dispensado/<int:pago_id>', methods=['POST'])
def marcar_dispensado(pago_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE pagos SET estado = 'dispensado' WHERE id = ?", (pago_id,))
    conn.commit()
    conn.close()
    return "Ok", 200

# ---------------- HOME ----------------
@app.route('/')
def home():
    return "Servidor Dispen-Easy funcionando (QR fijo por producto)."

# ---------------- MAIN ----------------
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
   
