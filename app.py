from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
import os
import mercadopago
from datetime import datetime

app = Flask(__name__)
CORS(app)

# -----------------------------
# CONFIG
# -----------------------------
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get("DATABASE_URL")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "adm123")
MP_TOKEN = os.environ.get("MP_ACCESS_TOKEN_TEST", "")

mp = mercadopago.SDK(MP_TOKEN)

# -----------------------------
# MODELOS
# -----------------------------
class Producto(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    slot = db.Column(db.Integer, nullable=False)
    nombre = db.Column(db.String(120))
    precio = db.Column(db.Float)
    dispenser = db.Column(db.String(40), default="dispen-01")

class Pago(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    mp_payment_id = db.Column(db.String(50))
    estado = db.Column(db.String(20))
    monto = db.Column(db.Float)
    slot = db.Column(db.Integer)
    producto = db.Column(db.String(120))
    dispenser = db.Column(db.String(40))
    fecha = db.Column(db.DateTime, default=datetime.utcnow)


# Crear DB si no existe
with app.app_context():
    db.create_all()
    # Crear los 2 productos iniciales si no existen
    if Producto.query.count() == 0:
        p1 = Producto(slot=1, nombre="Producto 1", precio=100)
        p2 = Producto(slot=2, nombre="Producto 2", precio=100)
        db.session.add_all([p1, p2])
        db.session.commit()


# -----------------------------
# MIDDLEWARE ADMIN
# -----------------------------
def require_admin():
    if request.headers.get("x-admin-secret") != ADMIN_SECRET:
        return False
    return True


# -----------------------------
# API PRODUCTOS
# -----------------------------
@app.get("/api/productos")
def api_productos():
    if not require_admin():
        return "forbidden", 403

    productos = Producto.query.order_by(Producto.slot).all()
    return jsonify([
        {
            "id": p.id,
            "slot": p.slot,
            "nombre": p.nombre,
            "precio": p.precio,
        } for p in productos
    ])


@app.post("/api/productos/<int:pid>/guardar")
def api_producto_guardar(pid):
    if not require_admin():
        return "forbidden", 403

    data = request.json
    p = Producto.query.get(pid)
    if not p:
        return jsonify({"error": "producto no existe"}), 404

    p.nombre = data.get("nombre", p.nombre)
    p.precio = data.get("precio", p.precio)
    db.session.commit()

    return jsonify({"ok": True})


# -----------------------------
# API MERCADOPAGO - GENERAR PREFERENCIA
# -----------------------------
@app.post("/api/pagos/preferencia")
def mp_preferencia():
    if not require_admin():
        return "forbidden", 403

    data = request.json
    pid = data.get("product_id")

    p = Producto.query.get(pid)
    if not p:
        return jsonify({"error": "producto no disponible"}), 400

    preference_data = {
        "items": [
            {"title": p.nombre, "quantity": 1, "unit_price": float(p.precio)}
        ],
        "metadata": {
            "slot": p.slot,
            "producto": p.nombre,
            "dispenser": p.dispenser
        }
    }

    pref = mp.preference().create(preference_data)
    init_point = pref["response"]["init_point"]

    return jsonify({"init_point": init_point})


# -----------------------------
# API PAGOS (LISTAR)
# -----------------------------
@app.get("/api/pagos")
def api_pagos():
    if not require_admin():
        return "forbidden", 403

    pagos = Pago.query.order_by(Pago.id.desc()).limit(10).all()
    return jsonify([
        {
            "id": p.id,
            "mp_payment_id": p.mp_payment_id,
            "estado": p.estado,
            "monto": p.monto,
            "slot": p.slot,
            "producto": p.producto,
            "dispenser": p.dispenser,
            "fecha": p.fecha.isoformat()
        }
        for p in pagos
    ])


@app.get("/")
def home():
    return "Backend Dispen-Easy funcionando"
