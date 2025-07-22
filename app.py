from flask import Flask, request, jsonify, redirect
from flask_cors import CORS
import sqlite3

app = Flask(__name__)
CORS(app)

DB_PATH = "productos.db"

# ---------------- INICIALIZAR TABLAS ----------------
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
    # Tabla de pagos registrados
    c.execute('''
        CREATE TABLE IF NOT EXISTS pagos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            producto_id INTEGER,
            estado TEXT,
            fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()
    return "Tablas inicializadas (productos, pagos)"

# ---------------- CRUD PRODUCTOS ----------------
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
    c.execute(
        "INSERT INTO productos (nombre, precio, link_pago) VALUES (?, ?, ?)",
        (nombre, precio, link_pago)
    )
    conn.commit()
    conn.close()
    return "Producto agregado", 201

@app.route('/productos/<int:producto_id>', methods=['DELETE'])
def delete_producto(producto_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM productos WHERE id=?", (producto_id,))
    conn.commit()
    conn.close()
    return "Producto eliminado", 200

# ---------------- REDIRECCIÓN A MERCADOPAGO ----------------
@app.route('/pagar/<int:producto_id>')
def pagar(producto_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT link_pago FROM productos WHERE id = ?", (producto_id,))
    row = c.fetchone()
    conn.close()
    if row and row[0]:
        return redirect(row[0])
    else:
        return "Link de pago no encontrado para este producto.", 404

# ---------------- WEBHOOK: REGISTRA PAGO ----------------
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json
    # Debe incluir: producto_id y estado ("aprobado" o lo que decidas)
    producto_id = data.get('producto_id')
    estado = data.get('estado', 'pendiente')
    if not producto_id:
        return "Falta producto_id", 400
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO pagos (producto_id, estado) VALUES (?, ?)",
        (producto_id, estado)
    )
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})

# ---------------- CONSULTA DE PAGOS PENDIENTES (ESP32) ----------------
@app.route('/pagos_pendientes', methods=['GET'])
def pagos_pendientes():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, producto_id FROM pagos WHERE estado = 'aprobado' ORDER BY fecha LIMIT 1")
    row = c.fetchone()
    conn.close()
    if row:
        return jsonify({'pago_id': row[0], 'producto_id': row[1]})
    else:
        return jsonify({'pago_id': None, 'producto_id': None})

# ---------------- MARCAR COMO DISPENSADO (ESP32) ----------------
@app.route('/marcar_dispensado', methods=['POST'])
def marcar_dispensado():
    data = request.json
    pago_id = data.get('pago_id')
    if not pago_id:
        return "Falta pago_id", 400
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE pagos SET estado='dispensado' WHERE id=?", (pago_id,))
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})

@app.route('/')
def home():
    return "Servidor Dispen-Easy funcionando."

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000)
