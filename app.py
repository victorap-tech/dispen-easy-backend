from flask import Flask, request, jsonify
import sqlite3
import requests

app = Flask(__name__)

DB_PATH = 'productos.db'
ACCESS_TOKEN = "APP_USR-7903926381447246-061121-b38fe6b7c7d58e0b3927c08d041e9bd9-246749043"  # Usa producción o test

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

@app.route('/initdb')
def init_db():
    conn = get_db_connection()
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
    # Tabla de pagos
    c.execute('''
        CREATE TABLE IF NOT EXISTS pagos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            producto_id INTEGER,
            mp_payment_id TEXT,
            estado TEXT,
            FOREIGN KEY(producto_id) REFERENCES productos(id)
        )
    ''')
    conn.commit()
    conn.close()
    return 'DB inicializada', 200

@app.route('/productos', methods=['GET'])
def productos():
    conn = get_db_connection()
    productos = conn.execute('SELECT * FROM productos').fetchall()
    conn.close()
    productos_list = []
    for p in productos:
        productos_list.append({
            'id': p['id'],
            'nombre': p['nombre'],
            'precio': p['precio'],
            'link_pago': p['link_pago']
        })
    return jsonify(productos_list)

@app.route('/productos', methods=['POST'])
def crear_producto():
    data = request.get_json()
    nombre = data.get('nombre')
    precio = float(data.get('precio'))
    # Crear link Mercado Pago
    mp_url = 'https://api.mercadopago.com/checkout/preferences'
    headers = {
        'Authorization': f'Bearer {ACCESS_TOKEN}',
        'Content-Type': 'application/json'
    }
    payload = {
        "items": [{
            "title": nombre,
            "quantity": 1,
            "currency_id": "ARS",
            "unit_price": precio
        }]
    }
    r = requests.post(mp_url, headers=headers, json=payload)
    if r.status_code != 201:
        return "Error generando link MP", 400
    link_pago = r.json()['init_point']
    # Guardar en BD
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('INSERT INTO productos (nombre, precio, link_pago) VALUES (?, ?, ?)', (nombre, precio, link_pago))
    conn.commit()
    conn.close()
    return jsonify({'nombre': nombre, 'precio': precio, 'link_pago': link_pago})

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json
    print("WEBHOOK:", data)
    mp_payment_id = data.get('data', {}).get('id')
    if not mp_payment_id:
        return "Sin ID de pago", 400
    # Consultar estado real
    url = f"https://api.mercadopago.com/v1/payments/{mp_payment_id}"
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
    r = requests.get(url, headers=headers)
    if r.status_code != 200:
        return "No se pudo consultar el pago", 400
    info = r.json()
    estado = info.get("status")
    # Puedes relacionar producto usando info['order']['items'] si lo guardas antes
    # Aquí sólo registro el pago
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT INTO pagos (mp_payment_id, estado) VALUES (?, ?)", (mp_payment_id, estado))
    conn.commit()
    conn.close()
    return "Pago registrado", 200

@app.route('/')
def home():
    return 'Servidor Dispen-Easy funcionando (con generación automática de links de pago Mercado Pago).'

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
