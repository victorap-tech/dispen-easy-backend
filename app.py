import os
import json
from datetime import datetime
from flask import Flask, request, jsonify, Response
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.orm import relationship
from sqlalchemy import func
from flask_cors import CORS
import requests

# --------------------------------------------------
# CONFIG
# --------------------------------------------------

ADMIN_SECRET = os.getenv("ADMIN_SECRET", "adm123")
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "")
BASE_URL = os.getenv("BACKEND_BASE_URL", "")  # Railway
MQTT_HOST = os.getenv("MQTT_HOST", "")
MQTT_TOPIC_PREFIX = "dispen"

app = Flask(__name__)
CORS(app)

db_url = os.getenv("DATABASE_URL")
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://")

app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)


# --------------------------------------------------
# MODELOS SIMPLIFICADOS
# --------------------------------------------------

class Dispenser(db.Model):
    __tablename__ = "dispenser"
    id = db.Column(db.Integer, primary_key=True)
    device_id = db.Column(db.String, unique=True)
    nombre = db.Column(db.String)
    activo = db.Column(db.Boolean, default=True)

    productos = relationship("Producto", backref="dispenser", cascade="all, delete-orphan")


class Producto(db.Model):
    __tablename__ = "producto"
    id = db.Column(db.Integer, primary_key=True)

    dispenser_id = db.Column(db.Integer, db.ForeignKey("dispenser.id"))
    slot = db.Column(db.Integer)  # 1 ó 2

    nombre = db.Column(db.String)
    precio = db.Column(db.Float)

    habilitado = db.Column(db.Boolean, default=True)


class Pago(db.Model):
    __tablename__ = "pago"
    id = db.Column(db.Integer, primary_key=True)

    dispenser_id = db.Column(db.Integer)
    product_id = db.Column(db.Integer)
    slot = db.Column(db.Integer)

    mp_payment_id = db.Column(db.String)
    monto = db.Column(db.Float)
    estado = db.Column(db.String)
    dispensado = db.Column(db.Boolean, default=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# --------------------------------------------------
# HELPERS
# --------------------------------------------------

def admin_required(req):
    sec = req.headers.get("x-admin-secret")
    if not sec or sec != ADMIN_SECRET:
        return False
    return True


# --------------------------------------------------
# CREAR TABLAS
# --------------------------------------------------
with app.app_context():
    db.create_all()


# --------------------------------------------------
# API
# --------------------------------------------------

@app.get("/api/dispensers")
def get_dispensers():
    if not admin_required(request):
        return jsonify({"error": "unauthorized"}), 401

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


@app.post("/api/dispensers")
def add_dispenser():
    if not admin_required(request):
        return jsonify({"error": "unauthorized"}), 401

    data = request.json
    device_id = data.get("device_id")
    nombre = data.get("nombre")

    d = Dispenser(device_id=device_id, nombre=nombre)
    db.session.add(d)
    db.session.commit()

    # Crear automáticamente 2 productos
    for i in [1, 2]:
        p = Producto(dispenser_id=d.id, slot=i, nombre="", precio=0, habilitado=True)
        db.session.add(p)
    db.session.commit()

    return jsonify({"ok": True, "dispenser": {
        "id": d.id,
        "device_id": d.device_id,
        "nombre": d.nombre,
        "activo": d.activo
    }})


@app.get("/api/productos")
def get_productos():
    if not admin_required(request):
        return jsonify({"error": "unauthorized"}), 401

    disp = request.args.get("dispenser_id")
    ps = Producto.query.filter_by(dispenser_id=disp).order_by(Producto.slot).all()

    out = []
    for p in ps:
        out.append({
            "id": p.id,
            "slot": p.slot,
            "dispenser_id": p.dispenser_id,
            "nombre": p.nombre,
            "precio": p.precio,
            "habilitado": p.habilitado
        })
    return jsonify(out)


@app.put("/api/productos/<int:pid>")
def update_prod(pid):
    if not admin_required(request):
        return jsonify({"error": "unauthorized"}), 401

    p = Producto.query.get(pid)
    if not p:
        return jsonify({"error": "no existe"}), 404

    data = request.json
    p.nombre = data.get("nombre", p.nombre)
    p.precio = data.get("precio", p.precio)
    p.habilitado = data.get("habilitado", p.habilitado)

    db.session.commit()
    return jsonify({"ok": True, "producto": {
        "id": p.id,
        "slot": p.slot,
        "dispenser_id": p.dispenser_id,
        "nombre": p.nombre,
        "precio": p.precio,
        "habilitado": p.habilitado
    }})


# --------------------------------------------------
# MERCADOPAGO
# --------------------------------------------------

@app.post("/api/pagos/preferencia")
def crear_preferencia():
    if not admin_required(request):
        return jsonify({"error": "unauthorized"}), 401

    data = request.json
    producto_id = data.get("product_id")

    p = Producto.query.get(producto_id)
    if not p or not p.habilitado:
        return jsonify({"error": "producto no disponible"}), 400

    monto = float(p.precio)

    # Crear preferencia MP
    url = "https://api.mercadopago.com/checkout/preferences"
    headers = {
        "Authorization": f"Bearer {MP_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }

    body = {
        "items": [{
            "title": p.nombre,
            "quantity": 1,
            "currency_id": "ARS",
            "unit_price": monto
        }],
        "notification_url": f"{BASE_URL}/api/webhook"
    }

    r = requests.post(url, headers=headers, json=body)
    pref = r.json()

    # Guardar pago preliminar
    pg = Pago(
        dispenser_id=p.dispenser_id,
        product_id=p.id,
        slot=p.slot,
        monto=monto,
        estado="pending",
        mp_payment_id=pref.get("id", "0")
    )
    db.session.add(pg)
    db.session.commit()

    return jsonify({
        "init_point": pref.get("init_point"),
        "preference_id": pref.get("id"),
        "pago_id": pg.id
    })


@app.post("/api/webhook")
def webhook():
    data = request.json or {}
    payment_id = str(data.get("data", {}).get("id"))

    if not payment_id:
        return jsonify({"error": "sin id"}), 200

    # Buscar Pago por mp_payment_id
    pg = Pago.query.filter_by(mp_payment_id=payment_id).first()
    if not pg:
        return jsonify({"msg": "no encontrado"}), 200

    # Pedir a MP detalles
    url = f"https://api.mercadopago.com/v1/payments/{payment_id}"
    headers = {"Authorization": f"Bearer {MP_ACCESS_TOKEN}"}
    r = requests.get(url, headers=headers)
    info = r.json()

    estado = info.get("status")
    pg.estado = estado
    db.session.commit()

    # Si aprobado -> enviar a MQTT
    if estado == "approved":
        topic = f"{MQTT_TOPIC_PREFIX}/{pg.dispenser_id}/dispense/{pg.slot}"
        print("MQTT:", topic)

    return jsonify({"ok": True})


# --------------------------------------------------
# PAGOS
# --------------------------------------------------

@app.get("/api/pagos")
def get_pagos():
    if not admin_required(request):
        return jsonify({"error": "unauthorized"}), 401

    q = Pago.query.order_by(Pago.id.desc()).limit(20).all()
    out = []
    for p in q:
        out.append({
            "id": p.id,
            "mp_payment_id": p.mp_payment_id,
            "estado": p.estado,
            "monto": p.monto,
            "slot": p.slot,
            "product_id": p.product_id,
            "dispenser_id": p.dispenser_id,
            "dispensado": p.dispensado,
            "created_at": p.created_at.isoformat()
        })
    return jsonify(out)


@app.post("/api/pagos/<int:pid>/reenviar")
def reenviar(pid):
    if not admin_required(request):
        return jsonify({"error": "unauthorized"}), 401

    p = Pago.query.get(pid)
    if not p or p.estado != "approved":
        return jsonify({"error": "no reenviable"}), 400

    topic = f"{MQTT_TOPIC_PREFIX}/{p.dispenser_id}/dispense/{p.slot}"
    print("REENVIAR MQTT:", topic)

    return jsonify({"msg": "reenviado"})


# --------------------------------------------------
# SSE ONLINE/OFFLINE (Simple)
# --------------------------------------------------

@app.get("/api/events/stream")
def sse():
    sec = request.args.get("secret")
    if sec != ADMIN_SECRET:
        return Response("unauthorized", status=401)

    def stream():
        yield "data: {}\n\n"
    return Response(stream(), mimetype="text/event-stream")


# --------------------------------------------------
# RUN
# --------------------------------------------------

@app.get("/")
def home():
    return "Dispen-Easy backend OK"
