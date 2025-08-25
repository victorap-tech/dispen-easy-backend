from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from datetime import datetime
import os

app = Flask(__name__)
CORS(app)

# Configuración base de datos Railway
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv("DATABASE_URL")
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# Modelo de Producto
class Producto(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(100), nullable=False)
    precio = db.Column(db.Integer, nullable=False)  # en centavos
    litros = db.Column(db.Float, nullable=False)    # litros que representa la compra
    stock_real = db.Column(db.Float, default=0)     # litros disponibles en tanque
    slot_id = db.Column(db.Integer, unique=True, nullable=False)
    habilitado = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

# Modelo de Pago
class Pago(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    producto_id = db.Column(db.Integer, db.ForeignKey("producto.id"), nullable=False)
    estado = db.Column(db.String(50), nullable=False, default="pendiente")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

# Ruta de verificación
@app.route("/")
def index():
    return "✅ Dispen-Easy funcionando"

# --- Endpoints API ---
@app.route("/api/productos", methods=["GET"])
def get_productos():
    productos = Producto.query.all()
    return jsonify([{
        "id": p.id,
        "nombre": p.nombre,
        "precio": p.precio,
        "litros": p.litros,
        "stock_real": p.stock_real,
        "slot_id": p.slot_id,
        "habilitado": p.habilitado
    } for p in productos])

@app.route("/api/productos", methods=["POST"])
def crear_producto():
    data = request.json
    nuevo = Producto(
        nombre=data["nombre"],
        precio=int(data["precio"]),
        litros=float(data["litros"]),
        stock_real=0,
        slot_id=int(data["slot_id"]),
        habilitado=True
    )
    db.session.add(nuevo)
    db.session.commit()
    return jsonify({"message": "Producto creado"}), 201

@app.route("/api/productos/<int:id>", methods=["DELETE"])
def eliminar_producto(id):
    producto = Producto.query.get(id)
    if not producto:
        return jsonify({"error": "No encontrado"}), 404
    db.session.delete(producto)
    db.session.commit()
    return jsonify({"message": "Producto eliminado"})

@app.route("/api/productos/<int:id>", methods=["PUT"])
def actualizar_producto(id):
    data = request.json
    producto = Producto.query.get(id)
    if not producto:
        return jsonify({"error": "No encontrado"}), 404
    if "habilitado" in data:
        producto.habilitado = data["habilitado"]
    if "precio" in data:
        producto.precio = int(data["precio"])
    db.session.commit()
    return jsonify({"message": "Producto actualizado"})

# Resetear tanque
@app.route("/api/productos/<int:id>/reset_stock", methods=["POST"])
def reset_stock(id):
    producto = Producto.query.get(id)
    if not producto:
        return jsonify({"error": "No encontrado"}), 404
    producto.stock_real = producto.litros  # capacidad nominal
    db.session.commit()
    return jsonify({"message": "Stock reseteado", "stock_real": producto.stock_real})

# Reponer stock manual
@app.route("/api/productos/<int:id>/reponer", methods=["POST"])
def reponer_stock(id):
    producto = Producto.query.get(id)
    if not producto:
        return jsonify({"error": "No encontrado"}), 404
    cantidad = request.json.get("cantidad", 0)
    producto.stock_real += float(cantidad)
    db.session.commit()
    return jsonify({"message": "Stock repuesto", "stock_real": producto.stock_real})

if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(host="0.0.0.0", port=5000)
