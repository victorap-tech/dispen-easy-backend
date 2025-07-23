from flask import Flask, request, jsonify
from flask_cors import CORS
import sqlite3
import time
import requests

app = Flask(__name__)
CORS(app)

DB_PATH = 'productos.db'

ACCESS_TOKEN = "TU_ACCESS_TOKEN_AQUI"  # ← Cambia esto por tu Access Token real de MP

# -------- INICIALIZAR TABLAS --------
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
            estado TEXT,
            fecha TEXT,
            id_pago_mercadopago TEXT,
            FOREIGN KEY (producto_id) REFERENCES productos(id)
        )
    ''')
    conn.commit()
    conn.close()
    return "Tablas inicializadas"

# -------- PRODUCTOS --------
@app.route('/productos', methods=['GET', 'POST'])
def productos():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if request.method == 'POST':
        data = request.json
        nombre = data.get('nombre')
        precio = float(data.get('precio'))
        # Crear link de pago con MercadoPago
        url_mp = "https://api.mercadopago.com/checkout/preferences"
        headers = {
            "Authorization": f"Bearer {ACCESS_TOKEN}",
            "Content-Type": "application/json"
        }
        payload = {
            "items": [{
                "title": nombre,
                "quantity": 1,
                "unit_price": precio
            }],
            "external_reference": nombre  # Podés usar el id si querés
        }
        resp = requests.post(url_mp, headers=headers, json=payload)
        if resp.status_code != 201:
            conn.close()
            return f"Error creando link de pago: {resp.text}", 500
        link_pago = resp.json().get("init_point")
        # Guardar producto en DB
        c.execute("INSERT INTO productos (nombre, precio, link_pago) VALUES (?, ?, ?)",
                  (nombre, precio, link_pago))
        conn.commit()
    c.execute("SELECT id, nombre, precio, link_pago FROM productos")
    productos = [{'id': row[0], 'nombre': row[1], 'precio': row[2], 'link_pago': row[3]} for row in c.fetchall()]
    conn.close()
    return jsonify(productos)

# -------- BORRAR PRODUCTO --------
@app.route('/productos/<int:id>', methods=['DELETE'])
def borrar_producto(id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM productos WHERE id=?", (id,))
    conn.commit()
    conn.close()
    return "Producto eliminado", 200

# -------- WEBHOOK DE MERCADOPAGO --------
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json
    mp_payment_id = data.get("data", {}).get("id")
    if not mp_payment_id:
        return "Sin ID de pago", 400
    # Consultar detalles del pago en MercadoPago
    url = f"https://api.mercadopago.com/v1/payments/{mp_payment_id}"
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}"
    }
    r = requests.get(url, headers=headers)
    if r.status_code != 200:
        return "No se pudo consultar el pago", 400
    payment_info = r.json()
    status = payment_info["status"]
    if status != "approved":
        return "Pago no aprobado", 200
    # Obtener external_reference para buscar el producto
    external_reference = payment_info.get("external_reference")
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id FROM productos WHERE nombre = ?", (external_reference,))
    row = c.fetchone()
    if not row:
        conn.close()
        return "Producto no encontrado", 404
    producto_id = row[0]
    # Registrar el pago como pendiente
    c.execute(
        "INSERT INTO pagos (producto_id, estado, fecha, id_pago_mercadopago) VALUES (?, ?, ?, ?)",
        (producto_id, "pendiente", time.strftime("%Y-%m-%d %H:%M:%S"), mp_payment_id)
    )
    conn.commit()
    conn.close()
    return "Pago registrado", 200

# -------- VER PAGOS --------
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

# -------- CONSULTAR PAGOS PENDIENTES POR PRODUCTO --------
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

# -------- MARCAR PAGO COMO DISPENSADO --------
@app.route('/marcar_dispensado/<int:pago_id>', methods=['POST'])
def marcar_dispensado(pago_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE pagos SET estado = 'dispensado' WHERE id = ?", (pago_id,))
    conn.commit()
    conn.close()
    return "Ok", 200

# -------- HOME --------
@app.route('/')
def home():
    return "Servidor Dispen-Easy funcionando (con generación automática de links de pago Mercado Pago)."

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
