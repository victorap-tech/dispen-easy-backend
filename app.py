import os
import uuid
from flask import Flask, request, jsonify, make_response
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
import requests
from datetime import datetime

# ======================
# Configuración
# ======================
app = Flask(__name__)
CORS(app)

app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "sqlite:///pagos.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

ADMIN_SECRET = os.getenv("ADMIN_SECRET", "1234")
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "TEST-xxxxxxxxxx")  # Token de MP

# ======================
# Modelos
# ======================
class Pago(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    mp_payment_id = db.Column(db.String(100))
    product_id = db.Column(db.Integer, db.ForeignKey("producto.id"))
    slot_id = db.Column(db.Integer)
    device_id = db.Column(db.String(50))
    monto = db.Column(db.Float)
    litros = db.Column(db.Integer)
    estado = db.Column(db.String(50))
    dispensado = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Producto(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    dispenser_id = db.Column(db.Integer, db.ForeignKey("dispenser.id"))
    slot = db.Column(db.Integer)
    nombre = db.Column(db.String(100))
    precio = db.Column(db.Float)
    cantidad = db.Column(db.Float)  # stock en litros
    porcion_litros = db.Column(db.Float, default=1)
    habilitado = db.Column(db.Boolean, default=True)


class Dispenser(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    device_id = db.Column(db.String(50), unique=True)
    nombre = db.Column(db.String(100))
    activo = db.Column(db.Boolean, default=True)


with app.app_context():
    db.create_all()

# ======================
# Helper
# ======================
def check_admin(req):
    secret = req.headers.get("x-admin-secret")
    return secret == ADMIN_SECRET


def mp_create_preference(producto: Producto, pago: Pago):
    url = "https://api.mercadopago.com/checkout/preferences"
    headers = {"Authorization": f"Bearer {MP_ACCESS_TOKEN}"}
    body = {
        "items": [
            {
                "title": producto.nombre,
                "quantity": 1,
                "currency_id": "ARS",
                "unit_price": float(producto.precio),
            }
        ],
        "external_reference": str(pago.id),
        "notification_url": request.url_root.rstrip("/") + "/webhook",
    }
    r = requests.post(url, json=body, headers=headers)
    return r.json()


# ======================
# Rutas públicas
# ======================

@app.get("/ui/seleccionar")
def ui_seleccionar():
    pid = request.args.get("pid")
    if not pid:
        return "Producto inválido", 400

    producto = Producto.query.get(pid)
    if not producto:
        return "Producto no encontrado", 404

    # Creamos un pago en la base
    nuevo_pago = Pago(
        mp_payment_id=None,
        product_id=producto.id,
        slot_id=producto.slot,
        device_id=producto.dispenser_id,
        monto=producto.precio,
        litros=producto.porcion_litros,
        estado="pending",
    )
    db.session.add(nuevo_pago)
    db.session.commit()

    pref = mp_create_preference(producto, nuevo_pago)
    mp_link = pref.get("init_point", "#")

    html = f"""
    <html>
      <head><title>Dispen-Easy</title></head>
      <body style='font-family:sans-serif;text-align:center;padding:40px'>
        <h1>{producto.nombre}</h1>
        <p>Precio: ${producto.precio} x {producto.porcion_litros} L</p>
        <a href="{mp_link}" style="background:#10b981;color:white;padding:12px 20px;
           border-radius:8px;text-decoration:none;font-weight:bold">Pagar ahora</a>
      </body>
    </html>
    """
    return make_response(html, 200)


@app.post("/webhook")
def webhook():
    data = request.json
    if not data:
        return "no data", 400

    mp_payment_id = str(data.get("data", {}).get("id"))
    if not mp_payment_id:
        return "ok", 200

    # Consultar pago real en MP
    r = requests.get(
        f"https://api.mercadopago.com/v1/payments/{mp_payment_id}",
        headers={"Authorization": f"Bearer {MP_ACCESS_TOKEN}"},
    )
    if r.status_code != 200:
        return "error mp", 400
    pago_info = r.json()

    ext_ref = pago_info.get("external_reference")
    estado = pago_info.get("status")

    pago = Pago.query.get(int(ext_ref)) if ext_ref else None
    if not pago:
        return "pago desconocido", 404

    pago.mp_payment_id = mp_payment_id
    pago.estado = estado
    db.session.commit()

    return "ok", 200


# ======================
# Rutas de Admin
# ======================

@app.get("/api/dispensers")
def get_dispensers():
    if not check_admin(request):
        return "forbidden", 403
    ds = Dispenser.query.all()
    return jsonify([{
        "id": d.id,
        "device_id": d.device_id,
        "nombre": d.nombre,
        "activo": d.activo
    } for d in ds])


@app.get("/api/productos")
def get_productos():
    if not check_admin(request):
        return "forbidden", 403
    did = request.args.get("dispenser_id")
    q = Producto.query
    if did:
        q = q.filter_by(dispenser_id=did)
    return jsonify([{
        "id": p.id,
        "slot": p.slot,
        "nombre": p.nombre,
        "precio": p.precio,
        "cantidad": p.cantidad,
        "porcion_litros": p.porcion_litros,
        "habilitado": p.habilitado
    } for p in q.all()])


@app.post("/api/productos")
def crear_producto():
    if not check_admin(request):
        return "forbidden", 403
    data = request.json
    p = Producto(
        dispenser_id=data["dispenser_id"],
        slot=data["slot"],
        nombre=data["nombre"],
        precio=data["precio"],
        cantidad=data["cantidad"],
        porcion_litros=data["porcion_litros"],
        habilitado=data.get("habilitado", True),
    )
    db.session.add(p)
    db.session.commit()
    return jsonify({"producto": {
        "id": p.id,
        "slot": p.slot,
        "nombre": p.nombre,
        "precio": p.precio,
        "cantidad": p.cantidad,
        "porcion_litros": p.porcion_litros,
        "habilitado": p.habilitado
    }})


@app.put("/api/productos/<int:pid>")
def actualizar_producto(pid):
    if not check_admin(request):
        return "forbidden", 403
    p = Producto.query.get(pid)
    if not p:
        return "not found", 404
    data = request.json
    p.nombre = data.get("nombre", p.nombre)
    p.precio = data.get("precio", p.precio)
    p.cantidad = data.get("cantidad", p.cantidad)
    p.porcion_litros = data.get("porcion_litros", p.porcion_litros)
    p.habilitado = data.get("habilitado", p.habilitado)
    db.session.commit()
    return jsonify({"producto": {
        "id": p.id,
        "slot": p.slot,
        "nombre": p.nombre,
        "precio": p.precio,
        "cantidad": p.cantidad,
        "porcion_litros": p.porcion_litros,
        "habilitado": p.habilitado
    }})


@app.get("/api/pagos")
def listar_pagos():
    if not check_admin(request):
        return "forbidden", 403
    limit = int(request.args.get("limit", 10))
    pagos = Pago.query.order_by(Pago.created_at.desc()).limit(limit).all()
    return jsonify([{
        "id": p.id,
        "mp_payment_id": p.mp_payment_id,
        "product_id": p.product_id,
        "slot_id": p.slot_id,
        "device_id": p.device_id,
        "monto": p.monto,
        "litros": p.litros,
        "estado": p.estado,
        "dispensado": p.dispensado,
        "created_at": p.created_at.isoformat()
    } for p in pagos])


@app.post("/api/pagos/<int:pid>/reenviar")
def reenviar_pago(pid):
    if not check_admin(request):
        return "forbidden", 403
    pago = Pago.query.get(pid)
    if not pago:
        return "not found", 404
    if pago.estado != "approved":
        return jsonify({"msg": "El pago no está aprobado"}), 400

    # Publicar a MQTT o similar (simulado)
    # En tu caso: enviar señal al ESP32 → se arma el slot correspondiente
    return jsonify({"msg": f"Reenviado al dispenser slot {pago.slot_id}"})


# ======================
# Run
# ======================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
