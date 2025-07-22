from flask import Flask, request, jsonify
import sqlite3

app = Flask(__name__)
DB_PATH = 'pagos.db'  # O el nombre de tu base real

# --- Productos CRUD ---

# GET - lista todos los productos
@app.route('/productos', methods=['GET'])
def get_productos():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, nombre, precio FROM productos")
    productos = [{"id": row[0], "nombre": row[1], "precio": row[2]} for row in c.fetchall()]
    conn.close()
    return jsonify(productos)

# POST - agrega un producto
@app.route('/productos', methods=['POST'])
def add_producto():
    data = request.get_json()
    nombre = data.get('nombre')
    precio = data.get('precio')
    if not nombre or precio is None:
        return jsonify({"error": "Faltan datos"}), 400
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO productos (nombre, precio) VALUES (?, ?)", (nombre, precio))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})

# DELETE - borra un producto por ID
@app.route('/productos/<int:id>', methods=['DELETE'])
def delete_producto(id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM productos WHERE id = ?", (id,))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})
