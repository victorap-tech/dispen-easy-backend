# app.py
import os
import json
import logging
from datetime import datetime

import requests
from flask import Flask, jsonify, request
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.dialects.postgresql import JSONB

# -------------------------------------------------
# Config
# -------------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "")           # token de PROD (o el que uses)
MP_WEBHOOK_URL = os.getenv("MP_WEBHOOK_URL", "")          # https://.../api/mp/webhook
MQTT_ENABLED = bool(os.getenv("MQTT_ENABLED", "0") == "1") # opcional

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL or "sqlite:///local.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

logging.basicConfig(level=logging.INFO)
app.logger.setLevel(logging.INFO)

# -------------------------------------------------
# Modelos
# -------------------------------------------------
class Producto(db.Model):
    __tablename__ = "producto"
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(100), nullable=False)
    precio = db.Column(db.Float, nullable=False)
    cantidad = db.Column(db.Float, nullable=False, default=0.0)  # litros
    slot_id = db.Column(db.Integer, nullable=False, unique=True)
    habilitado = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

class Pago(db.Model):
    __tablename__ = "pago"
    id = db.Column(db.Integer, primary_key=True)
    mp_payment_id = db.Column(db.String(120), nullable=False, unique=True, index=True)
    estado = db.Column(db.String(80), nullable=False)            # approved / rejected / pending...
    producto = db.Column(db.String(120), nullable=False)         # nombre del producto en ese momento
    procesado = db.Column(db.Boolean, nullable=False, default=False)
    slot_id = db.Column(db.Integer)
    monto = db.Column(db.Float)
    raw = db.Column(JSONB)                                       # payload completo de MP (opcional)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    product_id = db.Column(db.Integer)                            # id numérico del producto

# -------------------------------------------------
# Utils
# -------------------------------------------------
def mp_headers():
    return {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}

def publicar_orden_mqtt(order_id: int, slot: int, product_id: int, amount: float):
    # placeholder: sólo logea para esta versión
    if not MQTT_ENABLED:
        app.logger.warning("[MQTT] no configurado: no se publica orden.")
        return
    app.logger.info(f"[MQTT] publicar orden: id={order_id} slot={slot} product_id={product_id} amount={amount}")

# -------------------------------------------------
# Rutas básicas
# -------------------------------------------------
@app.get("/")
def root():
    return jsonify({"status": "ok", "message": "Backend Dispen-Easy operativo"})

# -------- Productos (CRUD mínimo) --------
@app.get("/api/productos")
def list_productos():
    r = Producto.query.order_by(Producto.id.asc()).all()
    return jsonify([{
        "id": x.id, "nombre": x.nombre, "precio": x.precio, "cantidad": x.cantidad,
        "slot": x.slot_id, "habilitado": x.habilitado
    } for x in r])

@app.post("/api/productos")
def create_producto():
    d = request.get_json(force=True) or {}
    p = Producto(
        nombre=str(d.get("nombre","")).strip(),
        precio=float(d.get("precio",0)),
        cantidad=float(d.get("cantidad",0)),
        slot_id=int(d.get("slot",1)),
        habilitado=bool(d.get("habilitado", False)),
    )
    db.session.add(p)
    db.session.commit()
    return jsonify({"ok": True, "producto": {
        "id": p.id, "nombre": p.nombre, "precio": p.precio, "cantidad": p.cantidad,
        "slot": p.slot_id, "habilitado": p.habilitado
    }}), 201

@app.put("/api/productos/<int:pid>")
def update_producto(pid):
    p = Producto.query.get_or_404(pid)
    d = request.get_json(force=True) or {}
    if "nombre" in d: p.nombre = str(d["nombre"]).strip()
    if "precio" in d: p.precio = float(d["precio"])
    if "cantidad" in d: p.cantidad = float(d["cantidad"])
    if "slot" in d: p.slot_id = int(d["slot"])
    if "habilitado" in d: p.habilitado = bool(d["habilitado"])
    db.session.commit()
    return jsonify({"ok": True, "producto": {
        "id": p.id, "nombre": p.nombre, "precio": p.precio, "cantidad": p.cantidad,
        "slot": p.slot_id, "habilitado": p.habilitado
    }})

@app.delete("/api/productos/<int:pid>")
def delete_producto(pid):
    p = Producto.query.get_or_404(pid)
    db.session.delete(p)
    db.session.commit()
    return jsonify({"ok": True}), 204

@app.post("/api/productos/<int:pid>/reponer")
def reponer(pid):
    p = Producto.query.get_or_404(pid)
    d = request.get_json(force=True) or {}
    litros = float(d.get("litros", 0))
    if litros > 0:
        p.cantidad = float(p.cantidad or 0) + litros
        db.session.commit()
    return jsonify({"ok": True, "producto": {
        "id": p.id, "nombre": p.nombre, "precio": p.precio, "cantidad": p.cantidad,
        "slot": p.slot_id, "habilitado": p.habilitado
    }})

@app.post("/api/productos/<int:pid>/reset_stock")
def reset_stock(pid):
    p = Producto.query.get_or_404(pid)
    d = request.get_json(force=True) or {}
    litros = float(d.get("litros", 0))
    if litros >= 0:
        p.cantidad = litros
        db.session.commit()
    return jsonify({"ok": True, "producto": {
        "id": p.id, "nombre": p.nombre, "precio": p.precio, "cantidad": p.cantidad,
        "slot": p.slot_id, "habilitado": p.habilitado
    }})

# -------- Crear preferencia (Checkout Pro) --------
@app.post("/api/pagos/preferencia")
def crear_preferencia():
    data = request.get_json(force=True) or {}
    product_id = int(data.get("product_id", 0))
    slot_id = int(data.get("slot_id", 0))
    prod = Producto.query.get_or_404(product_id)

    pref_body = {
        "items": [{
            "title": prod.nombre,
            "quantity": 1,
            "currency_id": "ARS",
            "unit_price": float(prod.precio),
        }],
        "back_urls": {
            "success": os.getenv("MP_BACK_SUCCESS", "https://www.mercadopago.com.ar"),
            "failure": os.getenv("MP_BACK_FAILURE", "https://www.mercadopago.com.ar"),
            "pending": os.getenv("MP_BACK_PENDING", "https://www.mercadopago.com.ar"),
        },
        "auto_return": "approved",
        # ojo: MP puede reenviar distinto topic; siempre verificamos por /payments/{id}
        "external_reference": f"{product_id}:{slot_id}",
        "metadata": { "product_id": product_id, "slot_id": slot_id },
    }
    if MP_WEBHOOK_URL:
        pref_body["notification_url"] = MP_WEBHOOK_URL

    r = requests.post(
        "https://api.mercadopago.com/checkout/preferences",
        headers=mp_headers(),
        json=pref_body, timeout=15
    )
    r.raise_for_status()
    pref = r.json()
    return jsonify({
        "ok": True,
        "pref_id": pref.get("id"),
        "init_point": pref.get("init_point"),
        "sandbox_init_point": pref.get("sandbox_init_point"),
    })

# -------------------------------------------------
# Webhook MP (con logs verbosos)
# -------------------------------------------------
@app.post("/api/mp/webhook")
def mp_webhook():
    try:
        args = request.args or {}
        body = request.get_json(silent=True) or {}

        # Logs crudos
        app.logger.info("[MP] raw args=%s", dict(args))
        app.logger.info("[MP] raw body=%s", body)

        topic = args.get("topic") or body.get("type") or ""
        live_mode = bool(body.get("live_mode", True))
        base_api = "https://api.mercadopago.com" if live_mode else "https://api.sandbox.mercadopago.com"
        app.logger.info("[MP] webhook topic=%s live_mode=%s", topic, live_mode)

        # Si vino merchant_order, traemos el/los payments y seguimos con el primero
        payment_id = args.get("id") or ((body.get("data") or {}).get("id"))
        if topic == "merchant_order" and not payment_id:
            merchant_order_id = args.get("id") or body.get("id")
            if merchant_order_id:
                r_mo = requests.get(
                    f"{base_api}/merchant_orders/{merchant_order_id}",
                    headers=mp_headers(), timeout=15
                )
                r_mo.raise_for_status()
                mo = r_mo.json()
                pays = mo.get("payments") or []
                if not pays:
                    app.logger.warning("[MP] merchant_order %s sin payments", merchant_order_id)
                    return "ok", 200
                payment_id = str(pays[0].get("id"))

        if not payment_id:
            app.logger.warning("[MP] No se encontró payment_id en la notificación")
            return "ok", 200

        # Idempotencia
        if Pago.query.filter_by(mp_payment_id=str(payment_id)).first():
            return "ok", 200

        # Traer pago
        r = requests.get(f"{base_api}/v1/payments/{payment_id}", headers=mp_headers(), timeout=15)
        r.raise_for_status()
        p = r.json()

        app.logger.info("[MP] payment payload: id=%s status=%s external_reference=%s total_paid_amount=%s metadata=%s",
                        p.get("id"),
                        p.get("status"),
                        p.get("external_reference"),
                        (p.get("transaction_details") or {}).get("total_paid_amount"),
                        p.get("metadata"))

        status = (p.get("status") or "").lower()
        if status != "approved":
            app.logger.info("[MP] pago %s con estado %s (se ignora)", payment_id, status)
            return "ok", 200

        # Extraer product_id/slot_id
        product_id, slot_id = 0, 0
        ext = p.get("external_reference") or ""
        try:
            if ":" in ext:
                a, b = ext.split(":", 1)
                product_id = int(a)
                slot_id = int(b)
        except Exception:
            pass
        if not product_id or not slot_id:
            md = p.get("metadata") or {}
            product_id = int(md.get("product_id", 0) or 0)
            slot_id = int(md.get("slot_id", 0) or 0)

        prod = Producto.query.get(product_id)
        monto = float((p.get("transaction_details") or {}).get("total_paid_amount", 0.0))

        # Registrar pago
        reg = Pago(
            mp_payment_id=str(payment_id),
            estado="approved",
            producto=prod.nombre if prod else (p.get("description") or "producto"),
            procesado=False,
            slot_id=slot_id or (prod.slot_id if prod else None),
            monto=monto or (prod.precio if prod else None),
            raw=p,  # dejar payload para debug
            product_id=product_id or (prod.id if prod else None),
        )
        db.session.add(reg)

        # Descontar 1 unidad (o lo que prefieras)
        if prod and prod.cantidad is not None:
            prod.cantidad = max(0.0, float(prod.cantidad) - 1.0)

        db.session.commit()

        # Publicar orden (opcional)
        publicar_orden_mqtt(order_id=reg.id, slot=reg.slot_id or 0, product_id=reg.product_id or 0, amount=reg.monto or 0.0)
        return "ok", 200

    except requests.HTTPError as e:
        app.logger.error("[MP] HTTPError: %s - %s", e, getattr(e, "response", None))
        return "ok", 200
    except Exception as e:
        app.logger.exception("[MP] Error webhook: %s", e)
        return "ok", 200

# -------------------------------------------------
# ACK opcional (si algún día el ESP confirma por HTTP)
# -------------------------------------------------
@app.post("/api/dispense/ack/<int:order_id>")
def ack(order_id):
    g = Pago.query.get_or_404(order_id)
    if not g.procesado:
        g.procesado = True
        db.session.commit()
    return jsonify({"ok": True})

# -------------------------------------------------
# Listado rápido de pagos (debug/admin)
# -------------------------------------------------
@app.get("/api/pagos")
def list_pagos():
    q = Pago.query.order_by(Pago.id.desc()).limit(50).all()
    return jsonify([{
        "id": r.id,
        "mp_payment_id": r.mp_payment_id,
        "estado": r.estado,
        "producto": r.producto,
        "product_id": r.product_id,
        "slot_id": r.slot_id,
        "monto": r.monto,
        "procesado": r.procesado,
        "created_at": r.created_at.isoformat(),
    } for r in q])

# -------------------------------------------------
# Inicialización
# -------------------------------------------------
def initialize_database():
    with app.app_context():
        db.create_all()
        app.logger.info("Tablas verificadas/creadas.")

if __name__ == "__main__":
    initialize_database()
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
