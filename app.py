# app.py
import os
from flask import Flask, jsonify, request
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from sqlalchemy import Boolean, Integer, Float, String
from sqlalchemy.exc import SQLAlchemyError

# ---------------------------
# Configuración de la app
# ---------------------------
app = Flask(__name__)

# DB: usa DATABASE_URL si existe (Railway), si no, SQLite local
db_url = os.getenv("DATABASE_URL", "sqlite:///data.db")
# Fix para URI de postgres antigua -> nueva (sqlalchemy)
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

# CORS: habilitá orígenes específicos si querés (p. ej. tu dominio del front)
CORS(app, resources={r"/api/*": {"origins": "*"}})

# ---------------------------
# Modelo
# ---------------------------
class Product(db.Model):
    __tablename__ = "products"
    id = db.Column(Integer, primary_key=True)
    name = db.Column(String(120), nullable=False)
    price = db.Column(Float, default=0)
    quantity = db.Column(Integer, default=0)   # "cantidad"
    slot = db.Column(Integer, default=1)
    active = db.Column(Boolean, default=True)  # "habilitado"

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "price": self.price,
            "quantity": self.quantity,
            "slot": self.slot,
            "active": self.active,
        }

# Crear tablas si no existen (en arranque)
with app.app_context():
    db.create_all()

# ---------------------------
# Helpers
# ---------------------------
def _validate_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default

def _validate_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default

# ---------------------------
# Rutas raíz / salud
# ---------------------------
@app.route("/")
def root():
    return "Dispen-Easy backend activo"

# ---------------------------
# Rutas en ESPAÑOL
# ---------------------------

# Listar productos
@app.route("/api/productos", methods=["GET"])
def productos_listar():
    prods = Product.query.order_by(Product.id.asc()).all()
    return jsonify([p.to_dict() for p in prods])

# Crear producto
@app.route("/api/productos", methods=["POST"])
def productos_crear():
    data = request.get_json(force=True) or {}
    nombre = (data.get("name") or data.get("nombre") or "").strip()

    if not nombre:
        return jsonify({"ok": False, "error": "name (nombre) requerido"}), 400

    precio = _validate_float(data.get("price") or data.get("precio"), 0.0)
    cantidad = _validate_int(data.get("quantity") or data.get("cantidad"), 0)
    slot = _validate_int(data.get("slot"), 1)
    habilitado = bool(data.get("active", True))

    try:
        pr = Product(
            name=nombre,
            price=precio,
            quantity=cantidad,
            slot=slot,
            active=habilitado,
        )
        db.session.add(pr)
        db.session.commit()
        return jsonify({"ok": True, "id": pr.id})
    except SQLAlchemyError as e:
        db.session.rollback()
        return jsonify({"ok": False, "error": str(e)}), 500

# Eliminar producto
@app.route("/api/productos/<int:pid>", methods=["DELETE"])
def productos_eliminar(pid: int):
    pr = Product.query.get(pid)
    if not pr:
        return jsonify({"ok": False, "error": "no encontrado"}), 404
    try:
        db.session.delete(pr)
        db.session.commit()
        return jsonify({"ok": True})
    except SQLAlchemyError as e:
        db.session.rollback()
        return jsonify({"ok": False, "error": str(e)}), 500

# Generar QR (placeholder)
# Acepta GET y POST por compatibilidad con el front
@app.route("/api/generar_qr/<int:product_id>", methods=["GET", "POST"])
def generar_qr(product_id: int):
    pr = Product.query.get(product_id)
    if not pr:
        return jsonify({"ok": False, "error": "no encontrado"}), 404
    # Acá iría la lógica real para generar el QR y devolver la URL/base64
    return jsonify({"ok": True, "product_id": product_id})

# ---------------------------
# Alias en INGLÉS (compatibilidad)
# ---------------------------
@app.route("/api/products", methods=["GET"])
def products_list():
    return productos_listar()

@app.route("/api/products", methods=["POST"])
def products_create():
    return productos_crear()

@app.route("/api/products/<int:pid>", methods=["DELETE"])
def products_delete(pid: int):
    return productos_eliminar(pid)

@app.route("/api/generate_qr/<int:product_id>", methods=["GET", "POST"])
def generate_qr(product_id: int):
    return generar_qr(product_id)

# ---------------------------
# Arranque local
# ---------------------------
if __name__ == "__main__":
    # Para correr local: python app.py
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port, debug=True)
