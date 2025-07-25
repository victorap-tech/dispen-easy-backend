from flask import Flask, request, jsonify
import sqlite3
import requests

app = Flask(__name__)

DB_PATH = 'productos.db'
ACCESS_TOKEN = "APP_USR-7903926381447246-061121-b38fe6b7c7d58e0b3927c08d041e9bd9-246749043"  # Coloca tu Access Token aquí

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
            mp_payment_id TEXT
        )
    ''')
    conn.commit()
    conn.close()

@app.route('/initdb')
def initdb():
    init_db()
    return "DB Inicializada"

@app.route('/agregar_producto', methods=['POST'])
def agregar_producto():
    data = request.json
    nombre = data['nombre']
    precio = data['precio']

    # Generar link de pago MercadoPago
    url = "https://api.mercadopago.com/checkout/preferences"
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "items": [
            {
                "title": nombre,
                "quantity": 1,
                "currency_id": "ARS",
                "unit_price": float(precio)
            }
        ]
    }
    response = requests.post(url, headers=headers, json=payload)
    link_pago = response.json()["init_point"]

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO productos (nombre, precio, link_pago) VALUES (?, ?, ?)", (nombre, precio, link_pago))
    conn.commit()
    conn.close()
    return jsonify({"mensaje": "Producto agregado", "link_pago": link_pago})

@app.route('/productos', methods=['GET'])
def productos():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, nombre, precio, link_pago FROM productos")
    productos = [{"id": row[0], "nombre": row[1], "precio": row[2], "link_pago": row[3]} for row in c.fetchall()]
    conn.close()
    return jsonify(productos)

@app.route('/webhook', methods=['POST'])
def webhook():
    print("WEBHOOK RECIBIDO:")
    print(request.data)
    data = request.json
    mp_payment_id = data.get("data", {}).get("id")
    if not mp_payment_id:
        return "Sin ID de pago", 400

    # Consultar estado real del pago en MP
    url = f"https://api.mercadopago.com/v1/payments/{mp_payment_id}"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
    r = requests.get(url, headers=headers)
    if r.status_code != 200:
        return "No se pudo consultar el pago", 400
    payment_info = r.json()
    status = payment_info["status"]

    # Registrar pago en DB
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # Puedes asociar a producto_id si guardás el external_reference en la preferencia
    c.execute("INSERT INTO pagos (producto_id, estado, mp_payment_id) VALUES (?, ?, ?)",
              (None, status, mp_payment_id))
    conn.commit()
    conn.close()

    return "Pago registrado", 200

if __name__ == '__main__':
    init_db()
    app.run(debug=True)
