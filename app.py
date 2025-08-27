# app.py
import os
import json
import logging
from datetime import datetime

import requests
from flask import Flask, jsonify, request
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from sqlalchemy.dialects.postgresql import JSONB

# -------------------------------------------------------------------
# Config
# -------------------------------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "").strip()            # TEST-... o PROD
MP_WEBHOOK_URL = os.getenv("MP_WEBHOOK_URL", "").strip()            # opcional (solo log)
MQTT_ENABLED = bool(os.getenv("MQTT_ENABLED", "0") == "1")          # hoy no lo usamos

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL or "sqlite:///local.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# CORS (admin en otro dominio)
CORS(app, resources={r"/api/*": {"origins": "*"}})

db = SQLAlchemy(app)

logging.basicConfig(level=logging.INFO)
app.logger.setLevel(logging.INFO)

# -------------------------------------------------------------------
# Modelos
# -------------------------------------------------------------------
class Producto(db.Model):
    __tablename__ = "producto"
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(100), nullable=False)
    precio = db.Column(db.Float, nullable=False)
    cantidad = db.Column(db.Integer, nullable=False)         # litros disponibles (entero)
    slot_id = db.Column(db.Integer, nullable=False, unique=True, index=True)
    created_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now(), nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), onupdate=db.func.now())
    habilitado = db.Column(db.Boolean, nullable=False, server_default=db.text("false"))

class Pago(db.Model):
    __tablename__ = "pago"
    id = db.Column(db.Integer, primary_key=True)
    mp_payment_id = db.Column(db.String(120), nullable=False, unique=True, index=True)
    estado = db.Column(db.String(80), nullable=False)
    producto = db.Column(db.String(120), nullable=False)
    dispensado = db.Column(db.Boolean, nullable=False, default=False)
    slot_id = db.Column(db.Integer, nullable=True)
    monto = db.Column(db.Integer, nullable=True)             # total_paid_amount (centavos o entero ARS)
    product_id = db.Column(db.Integer, nullable=True)
    raw = db.Column(JSONB, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now(), nullable=False)

with app.app_context():
    db.create_all()

# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------
def mp_headers():
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

# ----------------------- Productos CRUD ----------------------------
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
            nombre=str(data.get("nombre","")).strip(),
            precio=float(data.get("precio", 0)),
            cantidad=int(float(data.get("cantidad", 0))),
            slot_id=int(data.get("slot", 1)),
            habilitado=bool(data.get("habilitado", False)),
        )
        db.session.add(p)
        db.session.commit()
        return jsonify({"ok": True, "producto": {
            "id": p.id, "nombre": p.nombre, "precio": p.precio, "cantidad": p.cantidad,
            "slot": p.slot_id, "habilitado": p.habilitado
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
            if new_slot != p.slot_id:
                # asegurar unicidad de slot
                exists = Producto.query.filter(Producto.slot_id == new_slot, Producto.id != p.id).first()
                if exists:
                    return json_error("Slot ya asignado a otro producto", 409)
                p.slot_id = new_slot
        if "habilitado" in data: p.habilitado = bool(data["habilitado"])
        db.session.commit()
        return jsonify({"ok": True, "producto": {
            "id": p.id, "nombre": p.nombre, "precio": p.precio, "cantidad": p.cantidad,
            "slot": p.slot_id, "habilitado": p.habilitado
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
    return jsonify({"ok": True, "producto": {
        "id": p.id, "cantidad": p.cantidad, "slot": p.slot_id
    }})

@app.post("/api/productos/<int:pid>/reset_stock")
def productos_reset(pid):
    p = Producto.query.get_or_404(pid)
    data = request.get_json(force=True)
    litros = int(float(data.get("litros", 0)))
    if litros < 0:
        return json_error("Litros inválidos")
    p.cantidad = litros
    db.session.commit()
    return jsonify({"ok": True, "producto": {
        "id": p.id, "cantidad": p.cantidad, "slot": p.slot_id
    }})

# ----------------------- MercadoPago: crear preferencia -------------
@app.post("/api/pagos/preferencia")
def crear_preferencia():
    try:
        data = request.get_json(force=True)
        # Permitimos dos variantes desde el front:
        # a) { product_id }  → buscamos el producto
        # b) { producto, precio, slot_id }  → usamos lo enviado
        product_id = data.get("product_id")
        if product_id:
            p = Producto.query.get_or_404(int(product_id))
            title = p.nombre
            unit_price = float(p.precio)
            slot_id = p.slot_id
            product_id_num = p.id
        else:
            title = str(data.get("producto", "Producto")).strip() or "Producto"
            unit_price = float(data.get("precio", 0))
            slot_id = int(data.get("slot_id", 0)) or None
            product_id_num = int(data.get("product_id", 0)) or None

        if unit_price <= 0:
            return json_error("Precio inválido")

        base_body = {
            "items": [
                {
                    "title": title,
                    "quantity": 1,
                    "currency_id": "ARS",
                    "unit_price": unit_price
                }
            ],
            "statement_descriptor": "DISPEN-EASY",
            "auto_return": "approved",
        }

        # metadata útil: slot/producto
        base_body["metadata"] = {
            "slot_id": slot_id,
            "product_id": product_id_num,
            "producto": title
        }

        # (opcional) back_urls (no rompemos si el front no las necesita)
        base_body["back_urls"] = {
            "success": "https://dispen-easy-web-production.up.railway.app/",
            "failure": "https://dispen-easy-web-production.up.railway.app/",
            "pending": "https://dispen-easy-web-production.up.railway.app/"
        }

        r = requests.post(
            "https://api.mercadopago.com/checkout/preferences",
            headers=mp_headers(),
            json=base_body,
            timeout=25
        )
        app.logger.info(f"[MP] preferencia req → {base_body}")
        r.raise_for_status()
        pref = r.json()

        # Devolvemos ambos links si están
        link = pref.get("init_point") or pref.get("sandbox_init_point")
        return jsonify({
            "ok": True,
            "preference_id": pref.get("id"),
            "init_point": pref.get("init_point"),
            "sandbox_init_point": pref.get("sandbox_init_point"),
            "link": link
        })
    except requests.HTTPError as e:
        try:
            detail = e.response.json()
        except Exception:
            detail = e.response.text if e.response is not None else str(e)
        app.logger.exception(f"[MP] Error creando preferencia: {detail}")
        return json_error("MercadoPago rechazó la preferencia", 502, {"detail": detail})
    except Exception as e:
        app.logger.exception("[MP] Error inesperado en preferencia")
        return json_error("Error interno creando preferencia", 500, {"detail": str(e)})

# ----------------------- Webhook MP --------------------------------
@app.post("/api/mp/webhook")
@app.post("/webhook")  # alias por si tenés esta URL cargada en MP
def mp_webhook():
    """
    Soporta notificaciones con:
    - query params: ?topic=payment&type=payment&id=123...
    - body con resource / merchant_order
    Maneja live_mode para elegir api.mercadopago.com vs api.sandbox.mercadopago.com
    """
    try:
        args = request.args or {}
        body = request.get_json(silent=True) or {}

        topic = args.get("topic") or args.get("type") or body.get("type")
        payment_id = str(args.get("id") or "")
        merchant_order_id = None

        # live_mode (True=prod, False=sandbox)
        live_mode = bool(body.get("live_mode", True))
        base_api = "https://api.mercadopago.com" if live_mode else "https://api.sandbox.mercadopago.com"

        # A veces MP manda un body con 'resource' apuntando a merchant_order
        if body.get("resource") and "merchant_orders" in body.get("resource", ""):
            topic = "merchant_order"
            merchant_order_id = body["resource"].rstrip("/").split("/")[-1]

        app.logger.info(f"[MP] webhook topic={topic} live_mode={live_mode} args={dict(args)}")
        app.logger.info(f"[MP] raw body={body}")

        # Si topic==merchant_order buscamos el/los payments y tomamos el primero
        if topic == "merchant_order" and not payment_id:
            try:
                r_mo = requests.get(
                    f"{base_api}/merchant_orders/{merchant_order_id}",
                    headers=mp_headers(),
                    timeout=15
                )
                r_mo.raise_for_status()
                mo = r_mo.json()
                pays = mo.get("payments") or []
                if not pays:
                    app.logger.warning(f"[MP] merchant_order {merchant_order_id} sin payments")
                    return "ok", 200
                payment_id = str(pays[0].get("id"))
            except Exception as e:
                app.logger.exception("[MP] fallo consultando merchant_order")
                return "ok", 200

        if not payment_id:
            app.logger.warning("[MP] No se encontró payment_id en la notificación")
            return "ok", 200

        # Consultar el detalle del pago
        try:
            r_pay = requests.get(
                f"{base_api}/v1/payments/{payment_id}",
                headers=mp_headers(),
                timeout=20
            )
            r_pay.raise_for_status()
            pay = r_pay.json()
        except requests.HTTPError as e:
            app.logger.exception(f"[MP] HTTPError obteniendo pago {payment_id}")
            return "ok", 200
        except Exception:
            app.logger.exception(f"[MP] Error obteniendo pago {payment_id}")
            return "ok", 200

        status = str(pay.get("status", ""))
        description = (pay.get("description") or "")  # a veces lo usamos para producto
        metadata = pay.get("metadata") or {}
        slot_id = metadata.get("slot_id") or None
        product_id = metadata.get("product_id") or None
        total_paid = pay.get("transaction_details", {}).get("total_paid_amount")

        # Si no vino metadata, tratamos de inferir
        if not product_id and description:
            # En tu flujo podés setear 'description' con el nombre del producto
            prod = Producto.query.filter(Producto.nombre.ilike(description)).first()
            if prod:
                product_id = prod.id
                slot_id = slot_id or prod.slot_id

        # Upsert pago
        pago = Pago.query.filter_by(mp_payment_id=str(payment_id)).first()
        if not pago:
            pago = Pago(
                mp_payment_id=str(payment_id),
                estado=status,
                producto=description or (metadata.get("producto") or "Producto"),
                dispensado=False,
                slot_id=slot_id,
                monto=int(total_paid) if total_paid is not None else None,
                product_id=product_id,
                raw=pay
            )
            db.session.add(pago)
        else:
            pago.estado = status
            pago.monto = int(total_paid) if total_paid is not None else pago.monto
            pago.slot_id = slot_id or pago.slot_id
            pago.producto = description or pago.producto
            pago.product_id = product_id or pago.product_id
            pago.raw = pay

        db.session.commit()
        app.logger.info(f"[MP] guardado pago {payment_id} estado={status}")
        return "ok", 200

    except Exception as e:
        db.session.rollback()
        app.logger.exception("[MP] Error procesando webhook")
        return "ok", 200  # MP reintentará; no queremos 500

# -------------------------------------------------------------------
# Run (local)
# -------------------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
