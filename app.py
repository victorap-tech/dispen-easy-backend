import os
from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
import requests
from datetime import datetime

app = Flask(__name__)
CORS(app)

# ------------------------------
# CONFIG
# ------------------------------

DATABASE_URL = os.getenv("DATABASE_URL")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://")

app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

ADMIN_SECRET = os.getenv("ADMIN_SECRET", "adm123")
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "")

# ------------------------------
# MODELOS
# ------------------------------

class Dispenser(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(50), unique=True, nullable=False)


class Producto(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    dispenser_id = db.Column(db.Integer, db.ForeignKey('dispenser.id'), nullable=False)
    slot = db.Column(db.Integer, nullable=False)
    nombre = db.Column(db.String(120), default="")
    precio = db.Column(db.Integer, default=0)

    dispenser = db.relationship("Dispenser", backref="productos")


class Pago(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    mp_payment_id = db.Column(db.String(80))
    estado = db.Column(db.String(40))
    monto = db.Column(db.Integer)
    slot = db.Column(db.Integer)
    producto_nombre = db.Column(db.String(120))
    dispenser_code = db.Column(db.String(50))
    fecha = db.Column(db.DateTime, default=datetime.utcnow)

# ------------------------------
# INICIALIZACIÓN AUTOMÁTICA
# ------------------------------

@app.before_first_request
def setup():
    db.create_all()

    d = Dispenser.query.filter_by(code="dispen-01").first()
    if not d:
        d = Dispenser(code="dispen-01")
        db.session.add(d)
        db.session.commit()

    for s in [1, 2]:
        p = Producto.query.filter_by(dispenser_id=d.id, slot=s).first()
        if not p:
            nuevo = Producto(dispenser_id=d.id, slot=s, nombre="", precio=0)
            db.session.add(nuevo)
    db.session.commit()

# ------------------------------
# PRODUCTOS
# ------------------------------

@app.get("/api/productos")
def get_productos():
    if request.headers.get("x-admin-secret") != ADMIN_SECRET:
        return jsonify({"error": "no autorizado"}), 401

    d = Dispenser.query.filter_by(code="dispen-01").first()
    items = []
    for p in d.productos:
        items.append({
            "id": p.id,
            "slot": p.slot,
            "nombre": p.nombre,
            "precio": p.precio
        })
    return jsonify(items)


@app.post("/api/productos/<int:id>/guardar")
def guardar_producto(id):
    if request.headers.get("x-admin-secret") != ADMIN_SECRET:
        return jsonify({"error": "no autorizado"}), 401

    data = request.json
    p = Producto.query.get(id)
    if not p:
        return jsonify({"error": "no existe"}), 404

    p.nombre = data.get("nombre", p.nombre)
    p.precio = data.get("precio", p.precio)

    db.session.commit()
    return jsonify({"ok": True})

# ------------------------------
# MERCADOPAGO: CREAR PREFERENCIA
# ------------------------------

@app.post("/api/pagos/preferencia")
def crear_preferencia():
    if request.headers.get("x-admin-secret") != ADMIN_SECRET:
        return jsonify({"error": "no autorizado"}), 401

    data = request.json
    product_id = data.get("product_id")

    p = Producto.query.get(product_id)
    if not p or p.precio <= 0 or p.nombre.strip() == "":
        return jsonify({"error": "producto no disponible"}), 400

    url = "https://api.mercadopago.com/checkout/preferences"

    payload = {
        "items": [
            {
                "title": p.nombre,
                "quantity": 1,
                "unit_price": float(p.precio)
            }
        ],
        "external_reference": f"{p.dispenser.code}-{p.slot}",
        "notification_url": f"{request.url_root}api/webhook"
    }

    headers = {
        "Authorization": f"Bearer {MP_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }

    r = requests.post(url, json=payload, headers=headers)

    if r.status_code != 201:
        return jsonify({"error": "mp_error", "detalle": r.text}), 400

    data_mp = r.json()
    return jsonify({
        "init_point": data_mp.get("init_point"),
        "id": data_mp.get("id")
    })

# ------------------------------
# WEBHOOK MERCADOPAGO
# ------------------------------

@app.post("/api/webhook")
def webhook():
    data = request.json

    payment_id = data.get("data", {}).get("id")
    if not payment_id:
        return "ok", 200

    r = requests.get(
        f"https://api.mercadopago.com/v1/payments/{payment_id}",
        headers={"Authorization": f"Bearer {MP_ACCESS_TOKEN}"}
    )

    info = r.json()
    status = info.get("status")
    amount = info.get("transaction_amount")
    ref = info.get("external_reference", "")

    dispenser_code, slot = ref.split("-")
    slot = int(slot)

    d = Dispenser.query.filter_by(code=dispenser_code).first()
    p = Producto.query.filter_by(dispenser_id=d.id, slot=slot).first()

    pago = Pago(
        mp_payment_id=str(payment_id),
        estado=status,
        monto=amount,
        slot=slot,
        producto_nombre=p.nombre,
        dispenser_code=dispenser_code
    )
    db.session.add(pago)
    db.session.commit()

    return "ok", 200

# ------------------------------
# HISTORIAL PAGOS
# ------------------------------

@app.get("/api/pagos")
def pagos():
    if request.headers.get("x-admin-secret") != ADMIN_SECRET:
        return jsonify({"error": "no autorizado"}), 401

    lista = []
    for p in Pago.query.order_by(Pago.id.desc()).limit(10):
        lista.append({
            "id": p.id,
            "mp_payment_id": p.mp_payment_id,
            "estado": p.estado,
            "monto": p.monto,
            "slot": p.slot,
            "producto": p.producto_nombre,
            "dispenser": p.dispenser_code,
            "fecha": p.fecha.isoformat()
        })
    return jsonify(lista)


# ------------------------------
# RUN
# ------------------------------

@app.get("/")
def home():
    return "Dispen-Easy backend OK"


if __name__ == "__main__":
    app.run()
