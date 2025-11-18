from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
import mercadopago
import os
from datetime import datetime

app = Flask(__name__)
CORS(app)

# ------------------------------------
# CONFIG DB
# ------------------------------------
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///pagos.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

# ------------------------------------
# MODELOS
# ------------------------------------

class Dispenser(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    device_id = db.Column(db.String, unique=True)
    nombre = db.Column(db.String)
    activo = db.Column(db.Boolean, default=True)
    productos = db.relationship("Producto", backref="dispenser")

class Producto(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    dispenser_id = db.Column(db.Integer, db.ForeignKey("dispenser.id"))
    slot = db.Column(db.Integer)
    nombre = db.Column(db.String)
    precio = db.Column(db.Float)
    habilitado = db.Column(db.Boolean, default=False)

class Pago(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    mp_payment_id = db.Column(db.String)
    monto = db.Column(db.Float)
    estado = db.Column(db.String)
    slot_id = db.Column(db.Integer)
    product_id = db.Column(db.Integer)
    dispenser_id = db.Column(db.Integer)
    dispensado = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.String)

db.create_all()

# ------------------------------------
# MERCADOPAGO
# ------------------------------------
MP_TOKEN = os.getenv("MP_ACCESS_TOKEN_TEST", "")
sdk = mercadopago.SDK(MP_TOKEN)

# ------------------------------------
# LISTA DISPENSERS
# ------------------------------------
@app.route("/api/dispensers")
def dispensers():
    ds = Dispenser.query.all()
    out = []
    for d in ds:
        out.append({
            "id": d.id,
            "device_id": d.device_id,
            "nombre": d.nombre,
            "activo": d.activo
        })
    return jsonify(out)

# ------------------------------------
# LISTA PRODUCTOS POR DISPENSER
# ------------------------------------
@app.route("/api/productos")
def productos():
    disp = request.args.get("dispenser_id")
    prods = Producto.query.filter_by(dispenser_id=disp).order_by(Producto.slot).all()
    out = []
    for p in prods:
        out.append({
            "id": p.id,
            "slot": p.slot,
            "nombre": p.nombre,
            "precio": p.precio,
            "habilitado": p.habilitado
        })
    return jsonify(out)

# ------------------------------------
# CREAR PRODUCTO
# ------------------------------------
@app.route("/api/productos", methods=["POST"])
def crear_producto():
    data = request.get_json()
    p = Producto(
        dispenser_id=data["dispenser_id"],
        slot=data["slot"],
        nombre=data["nombre"],
        precio=data["precio"],
        habilitado=data.get("habilitado", False)
    )
    db.session.add(p)
    db.session.commit()
    return jsonify({"producto": {
        "id": p.id, "slot": p.slot, "nombre": p.nombre,
        "precio": p.precio, "habilitado": p.habilitado
    }})

# ------------------------------------
# EDITAR PRODUCTO
# ------------------------------------
@app.route("/api/productos/<int:pid>", methods=["PUT"])
def editar_producto(pid):
    data = request.get_json()
    p = Producto.query.get(pid)
    if not p:
        return jsonify({"error":"no existe"}), 404

    p.nombre = data.get("nombre", p.nombre)
    p.precio = data.get("precio", p.precio)
    p.habilitado = data.get("habilitado", p.habilitado)

    db.session.commit()
    return jsonify({"producto": {
        "id": p.id, "slot": p.slot, "nombre": p.nombre,
        "precio": p.precio, "habilitado": p.habilitado
    }})

# ------------------------------------
# GENERAR LINK DE MERCADOPAGO
# ------------------------------------
@app.route("/api/pagos/preferencia", methods=["POST"])
def generar_preferencia():
    data = request.get_json()
    producto = Producto.query.get(data["product_id"])

    if not producto or not producto.habilitado:
        return jsonify({"error": "producto no disponible"}), 400

    preference_data = {
        "items": [
            {
                "title": producto.nombre,
                "quantity": 1,
                "unit_price": float(producto.precio)
            }
        ],
        "back_urls": {
            "success": "https://google.com",
            "failure": "https://google.com",
            "pending": "https://google.com"
        },
        "auto_return": "approved",
    }

    pref = sdk.preference().create(preference_data)
    return jsonify({"init_point": pref["response"]["init_point"]})

# ------------------------------------
# PAGOS RECIENTES
# ------------------------------------
@app.route("/api/pagos")
def pagos():
    rows = Pago.query.order_by(Pago.id.desc()).limit(10).all()
    out = []
    for r in rows:
        out.append({
            "id": r.id,
            "mp_payment_id": r.mp_payment_id,
            "estado": r.estado,
            "monto": r.monto,
            "slot_id": r.slot_id,
            "product_id": r.product_id,
            "device_id": r.dispenser_id,
            "dispensado": r.dispensado,
            "created_at": r.created_at
        })
    return jsonify(out)

# ------------------------------------
# WEBHOOK MERCADOPAGO
# ------------------------------------
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json

    pago = Pago(
        mp_payment_id=data.get("id"),
        estado="approved",
        monto=data.get("monto", 0),
        slot_id=data.get("slot_id", 0),
        product_id=data.get("product_id", 0),
        dispenser_id=data.get("dispenser_id", 0),
        dispensado=False,
        created_at=datetime.now().isoformat()
    )
    db.session.add(pago)
    db.session.commit()

    return jsonify({"received": True})

# ------------------------------------
# REENVIAR
# ------------------------------------
@app.route("/api/pagos/<int:pid>/reenviar", methods=["POST"])
def reenviar(pid):
    p = Pago.query.get(pid)
    if not p:
        return jsonify({"error":"no existe"}),404
    if p.estado != "approved":
        return jsonify({"error":"no aprobado"}),400
    return jsonify({"msg":"reenviado"})

# ------------------------------------
# INICIO
# ------------------------------------
@app.route("/")
def home():
    return "Dispen-Easy Backend OK"

# ------------------------------------
# RUN LOCAL
# ------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
