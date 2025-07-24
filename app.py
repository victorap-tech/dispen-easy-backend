from flask import Flask, request, jsonify
import sqlite3
import requests

app = Flask(__name__)
DB_PATH = 'productos.db'
ACCESS_TOKEN = 'APP_USR-xxx'  # Poné tu token de producción o prueba de MercadoPago

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
    conn.commit()
    conn.close()

@app.route('/initdb')
def route_initdb():
    init_db()
    return "DB inicializada", 200

@app.route('/productos', methods=['GET'])
def get_productos():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, nombre, precio, link_pago FROM productos")
    productos = [{"id": x[0], "nombre": x[1], "precio": x[2], "link_pago": x[3]} for x in c.fetchall()]
    conn.close()
    return jsonify(productos)

@app.route('/productos', methods=['POST'])
def add_producto():
    data = request.json
    nombre = data.get("nombre")
    precio = float(data.get("precio"))
    # Crea preferencia MercadoPago
    mp_data = {
        "items": [{
            "title": nombre,
            "quantity": 1,
            "unit_price": precio
        }]
    }
    headers = {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    r = requests.post("https://api.mercadopago.com/checkout/preferences", json=mp_data, headers=headers)
    if r.status_code == 201:
        mp_link = r.json()["init_point"]
    else:
        mp_link = None

    # Guarda en base
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO productos (nombre, precio, link_pago) VALUES (?, ?, ?)", (nombre, precio, mp_link))
    conn.commit()
    conn.close()
    return jsonify({"msg": "Producto creado", "link_pago": mp_link})

@app.route('/')
def home():
    return "Backend Dispen-Easy funcionando."

if __name__ == '__main__':
    init_db()
    app.run(host="0.0.0.0", port=5000)
