import os
import io
from flask import Flask, jsonify, request, send_file
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from sqlalchemy import func
import qrcode
import mercadopago

# ----------------------------------------------------
# Configuración básica
# ----------------------------------------------------
app = Flask(__name__)
CORS(app)

# Base de datos: usa SQLite por defecto; si existe DATABASE_URL (Railway/Postgres) la toma
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///data.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

# ----------------------------------------------------
# Modelo de Producto
# ----------------------------------------------------
class Producto(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(120), nullable=False)
    precio = db.Column(db.Float, nullable=False)
    cantidad = db.Column(db.Integer, nullable=False)
    posicion = db.Column(db.Integer, nullable=False)
    habilitado = db.Column(db.Boolean, default=True)

def to_dict(self):
    return {
        "id": self.id,
        "name": self.nombre,
        "price": self.precio,
        "qty": self.cantidad,
        "slot": self.posicion,
        "active": self.habilitado,
    }
# Crear tablas si no existen
with app.app_context():
    db.create_all()

# ----------------------------------------------------
# Endpoints básicos
# ----------------------------------------------------
@app.route("/")
def root():
    return "✅ Backend de Dispen-Easy activo"


# ----------------------------------------------------
# CRUD de Productos
# ----------------------------------------------------
@app.route("/api/productos", methods=["GET"])
def listar_productos():
    productos = Producto.query.order_by(Producto.id.asc()).all()
    return jsonify([p.to_dict() for p in productos])


@app.route("/api/productos", methods=["POST"])
def products_create():
    data = request.get_json(force=True) or {}

    # Aceptar claves en español o inglés
    name = (data.get("name") or data.get("nombre") or "").strip()
    price = float(data.get("price") or data.get("precio") or 0)
    qty   = int(data.get("qty")   or data.get("cantidad") or 1)
    slot  = int(data.get("slot")  or data.get("posicion") or 1)

    # Habilitado / active: aceptar varias formas
    raw_active = data.get("active")
    if raw_active is None:
        raw_active = data.get("habilitado")
    if isinstance(raw_active, str):
        active = raw_active.strip().lower() in ("1", "true", "t", "yes", "si", "sí")
    else:
        active = bool(raw_active) if raw_active is not None else True

    if not name:
        return jsonify({"ok": False, "error": "name/nombre requerido"}), 400

    pr = Producto(
    nombre=data.get("nombre"),
    precio=float(data.get("precio")),
    cantidad=int(data.get("cantidad")),
    posicion=int(data.get("posicion")),
    habilitado=data.get("habilitado", True)
)
    db.session.add(pr)
    db.session.commit()
    return jsonify({"ok": True, "id": pr.id})


@app.route("/api/productos/<int:pid>", methods=["DELETE"])
def eliminar_producto(pid):
    producto = Producto.query.get(pid)
    if not producto:
        return jsonify({"ok": False, "error": "Producto no encontrado"}), 404

    db.session.delete(producto)
    db.session.commit()
    return jsonify({"ok": True})


# ----------------------------------------------------
# Generar QR con MercadoPago
# ----------------------------------------------------
@app.route("/api/generar_qr/<int:pid>", methods=["GET"])
def generar_qr(pid):
    # Buscar producto
    producto = Producto.query.get(pid)
    if not producto:
        return jsonify({"ok": False, "error": "El producto no existe"}), 404

    mp_token = os.getenv("MP_ACCESS_TOKEN")
    if not mp_token:
        return jsonify({"ok": False, "error": "Falta MP_ACCESS_TOKEN en variables de entorno"}), 500

    # Armar preferencia básica
    preferencia = {
        "items": [
            {
                "title": producto.nombre,
                "quantity": 1,
                "currency_id": os.getenv("MP_CURRENCY", "ARS"),
                "unit_price": float(producto.precio or 0),
            }
        ],
        "back_urls": {
            "success": os.getenv("MP_BACK_SUCCESS", "https://example.com/success"),
            "failure": os.getenv("MP_BACK_FAILURE", "https://example.com/failure"),
            "pending": os.getenv("MP_BACK_PENDING", "https://example.com/pending"),
        },
        "auto_return": "approved",
    }

    try:
        sdk = mercadopago.SDK(mp_token)
        result = sdk.preference().create(preferencia)

        if "response" not in result or "init_point" not in result["response"]:
            return jsonify({"ok": False, "error": "Respuesta inesperada de MercadoPago", "mp": result}), 502

        link_pago = result["response"]["init_point"]

        # Devolver link directo (más práctico para el front)
        return jsonify({"ok": True, "qr": link_pago})

    except Exception as e:
        return jsonify({"ok": False, "error": f"Error al generar QR en MP: {str(e)}"}), 500


# ----------------------------------------------------
# Arranque local
# ----------------------------------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
