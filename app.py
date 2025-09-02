# app.py
import os
import logging
from datetime import datetime

import requests
from flask import Flask, jsonify, request
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from flask_migrate import Migrate
from sqlalchemy.dialects.postgresql import JSONB

# -------------------------------------------------------------------
# Config
# -------------------------------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "").strip()
WEB_URL = os.getenv("WEB_URL", "https://dispen-easy.com")  # <--- Cambiá por tu front en producción

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL or "sqlite:///local.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# CORS (permite llamadas desde el frontend)
CORS(app, resources={r"/api/*": {"origins": "*"}})

db = SQLAlchemy(app)
migrate = Migrate(app, db)

logging.basicConfig(level=logging.INFO)
app.logger.setLevel(logging.INFO)

# -------------------------------------------------------------------
# Modelos
# -------------------------------------------------------------------
class Producto(db.Model):
    __tablename__ = "producto"
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(100), nullable=False)
    precio = db.Column(db.Float, nullable=False)              # precio por litro
    cantidad = db.Column(db.Integer, nullable=False)          # stock en litros
    slot_id = db.Column(db.Integer, nullable=False, unique=True, index=True)
    created_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now(), nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), onupdate=db.func.now())
    habilitado = db.Column(db.Boolean, nullable=False, server_default=db.text("false"))



class Pago(db.Model):
    __tablename__ = "pago"
    id = db.Column(db.Integer, primary_key=True)
    mp_payment_id = db.Column(db.String(120), nullable=False, unique=True, index=True)
    estado = db.Column(db.String(80), nullable=False)
    producto = db.Column(db.String(120), nullable=False)      # <-- TEXTO (NOT NULL)
    dispensado = db.Column(db.Boolean, nullable=False, default=False)
    slot_id = db.Column(db.Integer, nullable=True)
    monto = db.Column(db.Integer, nullable=True)
    raw = db.Column(JSONB, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now(), nullable=False)
    product_id = db.Column(db.Integer, nullable=True)         # <-- NOMBRE CORRECTO
    procesado = db.Column(db.Boolean, nullable=False, server_default=db.text("false"))
    litros = db.Column(db.Integer, nullable=True)             # ya existe en tu DB

# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------
def mp_auth_headers():
    if not ACCESS_TOKEN:
        raise RuntimeError("MP_ACCESS_TOKEN no configurado")
    return {"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"}

def json_error(message, status=400, extra=None):
    payload = {"error": message}
    if extra:
        payload.update(extra)
    return jsonify(payload), status

# -------------------------------------------------------------------
# Rutas básicas
# -------------------------------------------------------------------
@app.get("/")
def health():
    return jsonify({"status": "ok", "message": "Backend Dispen-Easy operativo"})

# ----------------------- CRUD Productos -----------------------------
@app.get("/api/productos")
def productos_list():
    prods = Producto.query.order_by(Producto.slot_id.asc()).all()
    return jsonify([
        {
            "id": p.id,
            "nombre": p.nombre,
            "precio": p.precio,
            "cantidad": p.cantidad,
            "slot": p.slot_id,
            "habilitado": bool(p.habilitado),
            "created_at": p.created_at.isoformat() if p.created_at else None,
            "updated_at": p.updated_at.isoformat() if p.updated_at else None,
        }
        for p in prods
    ])

@app.post("/api/productos")
def productos_create():
    data = request.get_json(force=True)
    try:
        p = Producto(
            nombre=str(data.get("nombre", "")).strip(),
            precio=float(data.get("precio", 0)),
            cantidad=int(float(data.get("cantidad", 0))),
            slot_id=int(data.get("slot", 1)),
            habilitado=bool(data.get("habilitado", False)),
        )
        db.session.add(p)
        db.session.commit()
        return jsonify({"ok": True, "producto": {
            "id": p.id, "nombre": p.nombre, "precio": p.precio, "cantidad": p.cantidad,
            "slot": p.slot_id, "habilitado": bool(p.habilitado)
        }}), 201
    except Exception as e:
        db.session.rollback()
        app.logger.exception("Error creando producto")
        return json_error("Error creando producto", 500, {"detail": str(e)})

@app.put("/api/productos/<int:pid>")
def productos_update(pid):
    data = request.get_json(force=True)
    p = Producto.query.get_or_404(pid)
    try:
        if "nombre" in data: p.nombre = str(data["nombre"]).strip()
        if "precio" in data: p.precio = float(data["precio"])
        if "cantidad" in data: p.cantidad = int(float(data["cantidad"]))
        if "slot" in data:
            new_slot = int(data["slot"])
            exists = Producto.query.filter(Producto.slot_id == new_slot, Producto.id != p.id).first()
            if exists:
                return json_error("Slot ya asignado a otro producto", 409)
            p.slot_id = new_slot
        if "habilitado" in data: p.habilitado = bool(data["habilitado"])
        db.session.commit()
        return jsonify({"ok": True, "producto": {
            "id": p.id, "nombre": p.nombre, "precio": p.precio, "cantidad": p.cantidad,
            "slot": p.slot_id, "habilitado": bool(p.habilitado)
        }})
    except Exception as e:
        db.session.rollback()
        app.logger.exception("Error actualizando producto")
        return json_error("Error actualizando producto", 500, {"detail": str(e)})

@app.delete("/api/productos/<int:pid>")
def productos_delete(pid):
    p = Producto.query.get_or_404(pid)
    try:
        db.session.delete(p)
        db.session.commit()
        return jsonify({"ok": True}), 204
    except Exception as e:
        db.session.rollback()
        app.logger.exception("Error eliminando producto")
        return json_error("Error eliminando producto", 500, {"detail": str(e)})

@app.post("/api/productos/<int:pid>/reponer")
def productos_reponer(pid):
    p = Producto.query.get_or_404(pid)
    data = request.get_json(force=True)
    litros = int(float(data.get("litros", 0)))
    if litros <= 0:
        return json_error("Litros inválidos")
    p.cantidad = max(0, p.cantidad + litros)
    db.session.commit()
    return jsonify({"ok": True, "producto": {"id": p.id, "cantidad": p.cantidad, "slot": p.slot_id}})

@app.post("/api/productos/<int:pid>/reset_stock")
def productos_reset(pid):
    p = Producto.query.get_or_404(pid)
    data = request.get_json(force=True)
    litros = int(float(data.get("litros", 0)))
    if litros < 0:
        return json_error("Litros inválidos")
    p.cantidad = litros
    db.session.commit()
    return jsonify({"ok": True, "producto": {"id": p.id, "cantidad": p.cantidad, "slot": p.slot_id}})

# ----------------------- Crear preferencia (MP) ---------------------
@app.post("/api/pagos/preferencia")
def crear_preferencia():
    data = request.get_json(force=True, silent=True) or {}
    product_id = int(data.get("product_id") or 0)
    litros = int(data.get("litros") or 1)

    prod = Producto.query.get(product_id)
    if not prod or not prod.habilitado:
        return jsonify({"error": "producto no disponible"}), 400

    total = float(prod.precio) * litros

    body = {
        "items": [{
            "title": f"{prod.nombre} ({litros}L)",
            "quantity": 1,
            "currency_id": "ARS",
            "unit_price": total,
        }],
        "metadata": {
            "slot_id": prod.slot_id,
            "product_id": prod.id,
            "producto": prod.nombre,
            "litros": litros,
        },
        "auto_return": "approved",
        "back_urls": {"success": WEB_URL, "failure": WEB_URL, "pending": WEB_URL},
        "notification_url": f"{request.url_root.rstrip('/')}/api/mp/webhook",
        "statement_descriptor": "DISPEN-EASY",
    }

    r = requests.post(
        "https://api.mercadopago.com/checkout/preferences",
        headers=mp_auth_headers(),
        json=body, timeout=20
    )
    try:
        r.raise_for_status()
    except Exception:
        app.logger.exception("[MP] error al crear preferencia: %s %s", r.status_code, r.text[:400])
        return jsonify({"error": "mp_preference_failed", "status": r.status_code, "body": r.text}), 500

    pref = r.json() or {}
    link = pref.get("init_point") or pref.get("sandbox_init_point")

    # Guarda un registro preliminar en la DB
    pago = Pago(
        mp_payment_id=str(pref.get("id")),
        estado="pending",
        producto_id=prod.id,
        slot_id=prod.slot_id,
        litros=litros,
        monto=int(round(total)),
        dispensado=False,
    )
    db.session.add(pago)
    db.session.commit()

    return jsonify({"ok": True, "link": link, "raw": pref})

# ----------------------- Webhook MP --------------------------------
@app.post("/api/mp/webhook")
def mp_webhook():
    body = request.get_json(silent=True) or {}
    args = request.args or {}
    topic = args.get("topic") or body.get("type")
    live_mode = bool(body.get("live_mode", True))
    base_api = "https://api.mercadopago.com" if live_mode else "https://api.sandbox.mercadopago.com"

    payment_id = None
    if topic == "payment":
        payment_id = (body.get("data") or {}).get("id") or args.get("id")
    elif topic == "merchant_order":
        mo_id = args.get("id") or (body.get("data") or {}).get("id")
        if mo_id:
            r_mo = requests.get(f"{base_api}/merchant_orders/{mo_id}",
                                headers={"Authorization": f"Bearer {ACCESS_TOKEN}"}, timeout=15)
            if r_mo.ok:
                pays = (r_mo.json() or {}).get("payments") or []
                if pays:
                    payment_id = str(pays[0].get("id"))

    if not payment_id:
        app.logger.warning("[MP] webhook sin payment_id")
        return "ok", 200

    r_pay = requests.get(f"{base_api}/v1/payments/{payment_id}",
                         headers={"Authorization": f"Bearer {ACCESS_TOKEN}"}, timeout=15)
    try:
        r_pay.raise_for_status()
    except Exception:
        app.logger.exception("[MP] HTTPError get payment: %s %s", r_pay.status_code, r_pay.text[:400])
        return "ok", 200

    pay = r_pay.json() or {}
    meta = pay.get("metadata") or {}
    estado = pay.get("status") or "pending"
    monto = int(round(float(pay.get("transaction_amount") or 0)))

    # nombre del producto (puede venir en description o items[0].title)
    producto_txt = (
        pay.get("description")
        or ((pay.get("additional_info") or {}).get("items") or [{}])[0].get("title")
        or str(meta.get("producto") or "")
    )[:120]

    # valores de metadata
    slot_id = int(meta.get("slot_id") or 0)
    litros = int(meta.get("litros") or 1)
    product_id = int(meta.get("product_id") or 0)

    # UPSERT por mp_payment_id
    p = Pago.query.filter_by(mp_payment_id=str(payment_id)).first()
    if not p:
        p = Pago(
            mp_payment_id=str(payment_id),
            estado=estado,
            producto=producto_txt,     # <-- OBLIGATORIO (NOT NULL)
            dispensado=False,
            slot_id=slot_id,
            monto=monto,
            raw=pay,
            product_id=product_id,     # <-- nombre correcto
            litros=litros,
        )
        db.session.add(p)
    else:
        p.estado = estado
        p.monto = monto
        p.producto = producto_txt
        p.slot_id = slot_id
        p.product_id = product_id
        p.litros = litros
        p.raw = pay

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        app.logger.exception("[DB] error guardando pago %s", payment_id)

    return "ok", 200

# -------------------------------------------------------------------
# Run local
# -------------------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
