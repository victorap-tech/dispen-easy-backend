import sqlite3
from flask import Flask, request, jsonify
import requests
import os

app = Flask(__name__)

DB_PATH = "productos.db"

# ¡IMPORTANTE! Poné tu access token de producción acá:
ACCESS_TOKEN = "APP_USR-7903926381447246-061121-b38fe6b7c7d58e0b3927c08d041e9bd9-246749043"  # Reemplazá por tu token de PRODUCCIÓN

# ---- CREAR/INICIALIZAR LAS TABLAS ----
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

# ---- CREAR PRODUCTO Y GENERAR LINK DE PAGO CON LA API ----
@app.route('/productos', methods=['POST'])
def crear_producto():
    data = request.json
    nombre = data['nombre']
    precio = data['precio']

    # Generar link de pago con la API de MercadoPago (Checkout Pro)
    preference_payload = {
        "items": [
            {
                "title": nombre,
                "quantity": 1,
                "currency_id": "ARS",
                "unit_price": float(precio)
            }
        ],
        "external_reference": nombre  # Podés mejorar esto según tu necesidad
    }
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    mp_response = requests.post(
        "https://api.mercadopago.com/checkout/preferences",
        headers=headers,
        json=preference_payload
    )
    if mp_response.status_code != 201:
        return jsonify({"error": "No se pudo generar el link de pago", "detalle": mp_response.text}), 400

    link_pago = mp_response.json()["init_point"]

    # Guardar en la base
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO productos (nombre, precio, link_pago) VALUES (?, ?, ?)",
        (nombre, precio, link_pago)
    )
    conn.commit()
    conn.close()

    return jsonify({"nombre": nombre, "precio": precio, "link_pago": link_pago})

# ---- LISTAR PRODUCTOS ----
@app.route('/productos', methods=['GET'])
def listar_productos():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM productos")
    productos = [
        {"id": row[0], "nombre": row[1], "precio": row[2], "link_pago": row[3]}
        for row in c.fetchall()
    ]
    conn.close()
    return jsonify(productos)

# ---- WEBHOOK PARA PAGOS ----
@app.route('/webhook', methods=['POST'])
def webhook():
    print("\n--- WEBHOOK RECIBIDO ---", flush=True)
    print("HEADERS:", request.headers, flush=True)
    print("RAW DATA:", request.data, flush=True)
    try:
        print("JSON:", request.json, flush=True)
    except Exception as e:
        print("Error al parsear JSON:", e, flush=True)

    data = request.json
    mp_payment_id = data.get("data", {}).get("id")
    if not mp_payment_id:
        return "Sin ID de pago", 400

    # Consultar el pago a la API de MercadoPago
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}"
    }
    mp_payment = requests.get(
        f"https://api.mercadopago.com/v1/payments/{mp_payment_id}",
        headers=headers
    )
    if mp_payment.status_code != 200:
        return "No se pudo consultar el pago", 400

    payment_info = mp_payment.json()
    status = payment_info["status"]
    external_reference = payment_info.get("external_reference", "")

    # Buscar el producto asociado por nombre
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id FROM productos WHERE nombre = ?", (external_reference,))
    row = c.fetchone()
    if not row:
        conn.close()
        return "Producto no encontrado", 400
    producto_id = row[0]

    # Registrar pago en la base
    c.execute(
        "INSERT INTO pagos (producto_id, estado, fecha, id_pago_mercadopago) VALUES (?, ?, datetime('now'), ?)",
        (producto_id, status, mp_payment_id)
    )
    conn.commit()
    conn.close()
    print("Pago registrado", flush=True)

    return "Pago registrado", 200

# ---- LISTAR PAGOS ----
@app.route('/pagos', methods=['GET'])
def listar_pagos():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        SELECT pagos.id, productos.nombre, pagos.estado, pagos.fecha, pagos.id_pago_mercadopago
        FROM pagos
        JOIN productos ON pagos.producto_id = productos.id
        ORDER BY pagos.fecha DESC
    ''')
    pagos = [
        {"id": row[0], "producto": row[1], "estado": row[2], "fecha": row[3], "id_pago_mercadopago": row[4]}
        for row in c.fetchall()
    ]
    conn.close()
    return jsonify(pagos)

# ---- HOME ----
@app.route('/')
def home():
    return "Servidor Dispen-Easy funcionando (con generación automática de links de pago Mercado Pago)."

# ---- RUN ----
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), debug=True)
