import os
import io
from flask import Flask, jsonify, request, send_file
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from sqlalchemy import func
import qrcode
import mercadopago

# -------------------------------------------------
# Configuración básica
# -------------------------------------------------
app = Flask(__name__)
CORS(app)

# DB: usa SQLite por defecto; si tienes DATABASE_URL la toma (Railway/Postgres)
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///data.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

# -------------------------------------------------
# Modelo
# -------------------------------------------------
class Product(db.Model):
    __tablename__ = "products"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(180), nullable=False)
    price = db.Column(db.Float, default=0)
    qty = db.Column(db.Integer, default=1)
    slot = db.Column(db.Integer, default=1)
    active = db.Column(db.Boolean, default=True)

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "price": self.price,
            "qty": self.qty,
            "slot": self.slot,
            "active": self.active,
        }

# Crea tablas si no existen
with app.app_context():
    db.create_all()

# -------------------------------------------------
# Salud
# -------------------------------------------------
@app.route("/")
def root():
    return "Dispen-Easy backend activo"

# -------------------------------------------------
# Productos
# -------------------------------------------------
@app.route("/api/products", methods=["GET"])
def products_list():
    prods = Product.query.order_by(Product.id.asc()).all()
    return jsonify([p.to_dict() for p in prods])

@app.route("/api/products", methods=["POST"])
def products_create():
    data = request.get_json(force=True) or {}
    name = (data.get("name") or "").strip()
    price = float(data.get("price") or 0)
    qty = int(data.get("qty") or 1)
    slot = int(data.get("slot") or 1)
    active = bool(data.get("active") if data.get("active") is not None else True)

    if not name:
        return jsonify({"ok": False, "error": "name requerido"}), 400

    pr = Product(name=name, price=price, qty=qty, slot=slot, active=active)
    db.session.add(pr)
    db.session.commit()
    return jsonify({"ok": True, "id": pr.id})

@app.route("/api/products/<int:pid>", methods=["DELETE"])
def products_delete(pid):
    pr = Product.query.get(pid)
    if not pr:
        return jsonify({"ok": False, "error": "no encontrado"}), 404
    db.session.delete(pr)
    db.session.commit()
    return jsonify({"ok": True})

# -------------------------------------------------
# Generar QR (MercadoPago -> PNG)
# -------------------------------------------------
@app.route("/api/generar_qr/<int:product_id>", methods=["GET"])
def generar_qr(product_id):
    """
    Crea una preferencia en MercadoPago para el producto y devuelve
    un PNG con el código QR que representa la URL de pago (init_point).
    """
    # Buscar producto
    producto = Product.query.get(product_id)
    if not producto:
        return jsonify({"ok": False, "error": "producto no existe"}), 404

    mp_token = os.getenv("MP_ACCESS_TOKEN")
    if not mp_token:
        return jsonify({"ok": False, "error": "Falta MP_ACCESS_TOKEN en variables de entorno"}), 500

    # Construir preferencia básica
    preference_data = {
        "items": [
            {
                "title": producto.name,
                "quantity": 1,
                "currency_id": os.getenv("MP_CURRENCY", "ARS"),
                "unit_price": float(producto.price or 0),
            }
        ],
        "back_urls": {
            "success": os.getenv("MP_BACK_SUCCESS", "https://example.com/success"),
            "failure": os.getenv("MP_BACK_FAILURE", "https://example.com/failure"),
            "pending": os.getenv("MP_BACK_PENDING", "https://example.com/pending"),
        },
        "auto_return": "approved",
    }

    # Crear preferencia en MP
    try:
        sdk = mercadopago.SDK(mp_token)
        result = sdk.preference().create(preference_data)
        if "response" not in result or "init_point" not in result["response"]:
            return jsonify({"ok": False, "error": "Respuesta inesperada de MercadoPago", "mp": result}), 502
        init_point = result["response"]["init_point"]
    except Exception as e:
        return jsonify({"ok": False, "error": f"Error MercadoPago: {str(e)}"}), 502

    # Generar imagen PNG del QR desde la URL
    try:
        img = qrcode.make(init_point)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        # devuelve la imagen para <img src="/api/generar_qr/ID">
        return send_file(buf, mimetype="image/png")
    except Exception as e:
        return jsonify({"ok": False, "error": f"No se pudo generar el QR: {str(e)}"}), 500

# -------------------------------------------------
# Arranque local
# -------------------------------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
