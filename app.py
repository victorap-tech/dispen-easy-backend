# app.py
import os
import logging
from datetime import datetime

import requests
from flask import Flask, jsonify, request
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError

# -------------------------------------------------------------
# Config
# -------------------------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL", "")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "").strip()
BACKEND_BASE_URL = os.getenv("BACKEND_BASE_URL", "").rstrip("/")  # ej: https://web-xxxx.up.railway.app
WEB_URL = os.getenv("WEB_URL", "https://example.com").strip()

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL or "sqlite:///local.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# CORS para /api/*
CORS(app, resources={r"/api/*": {"origins": "*"}})

db = SQLAlchemy(app)
logging.basicConfig(level=logging.INFO)
app.logger.setLevel(logging.INFO)

# -------------------------------------------------------------
# Modelos
# -------------------------------------------------------------
class Producto(db.Model):
    __tablename__ = "producto"
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(100), nullable=False)
    precio = db.Column(db.Float, nullable=False)                # $ por litro
    cantidad = db.Column(db.Integer, nullable=False)            # stock disponible (L)
    slot_id = db.Column(db.Integer, nullable=False, unique=True, index=True)
    porcion_litros = db.Column(db.Integer, nullable=False, server_default="1")  # litros a dispensar por venta
    created_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now(), nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), onupdate=db.func.now())
    habilitado = db.Column(db.Boolean, nullable=False, server_default=db.text("false"))

class Pago(db.Model):
    __tablename__ = "pago"
    id = db.Column(db.Integer, primary_key=True)
    mp_payment_id = db.Column(db.String(120), nullable=False, unique=True, index=True)
    estado = db.Column(db.String(80), nullable=False)           # approved/pending/rejected
    producto = db.Column(db.String(120), nullable=False, default="")
    dispensado = db.Column(db.Boolean, nullable=False, server_default=db.text("false"))
    slot_id = db.Column(db.Integer, nullable=False, default=0)
    litros = db.Column(db.Integer, nullable=False, default=1)
    monto = db.Column(db.Integer, nullable=False, default=0)    # ARS entero
    product_id = db.Column(db.Integer, nullable=False, default=0)
    raw = db.Column(JSONB, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now(), nullable=False)

# -------------------------------------------------------------
# Helpers
# -------------------------------------------------------------
def ok_json(data, status=200):
    return jsonify(data), status

def err_json(msg, status=400, detail=None):
    payload = {"error": msg}
    if detail is not None:
        payload["detail"] = detail
    return jsonify(payload), status

# -------------------------------------------------------------
# Health
# -------------------------------------------------------------
@app.get("/")
def health():
    return ok_json({"status": "ok", "message": "Backend Dispen-Easy operativo"})

# -------------------------------------------------------------
# Productos CRUD
# -------------------------------------------------------------
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
            "porcion_litros": p.porcion_litros,
            "habilitado": bool(p.habilitado),
            "created_at": p.created_at.isoformat() if p.created_at else None,
            "updated_at": p.updated_at.isoformat() if p.updated_at else None,
        } for p in prods
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
            porcion_litros=int(data.get("porcion_litros", 1)),
            habilitado=bool(data.get("habilitado", False)),
        )
        if p.precio < 0 or p.cantidad < 0 or p.porcion_litros < 1:
            return err_json("Valores inválidos", 400)
        if Producto.query.filter(Producto.slot_id == p.slot_id).first():
            return err_json("Slot ya asignado a otro producto", 409)

        db.session.add(p)
        db.session.commit()
        return ok_json({"ok": True, "producto": {
            "id": p.id, "nombre": p.nombre, "precio": p.precio, "cantidad": p.cantidad,
            "slot": p.slot_id, "porcion_litros": p.porcion_litros, "habilitado": bool(p.habilitado)
        }}, 201)
    except Exception as e:
        db.session.rollback()
        app.logger.exception("Error creando producto")
        return err_json("Error creando producto", 500, str(e))

@app.put("/api/productos/<int:pid>")
def productos_update(pid):
    data = request.get_json(force=True)
    p = Producto.query.get_or_404(pid)
    try:
        if "nombre" in data: p.nombre = str(data["nombre"]).strip()
        if "precio" in data: p.precio = float(data["precio"])
        if "cantidad" in data: p.cantidad = int(float(data["cantidad"]))
        if "porcion_litros" in data:
            val = int(data["porcion_litros"])
            if val < 1: return err_json("porcion_litros debe ser ≥ 1", 400)
            p.porcion_litros = val
        if "slot" in data:
            new_slot = int(data["slot"])
            if new_slot != p.slot_id and \
               Producto.query.filter(Producto.slot_id == new_slot, Producto.id != p.id).first():
                return err_json("Slot ya asignado a otro producto", 409)
            p.slot_id = new_slot
        if "habilitado" in data: p.habilitado = bool(data["habilitado"])

        db.session.commit()
        return ok_json({"ok": True, "producto": {
            "id": p.id, "nombre": p.nombre, "precio": p.precio, "cantidad": p.cantidad,
            "slot": p.slot_id, "porcion_litros": p.porcion_litros, "habilitado": bool(p.habilitado)
        }})
    except Exception as e:
        db.session.rollback()
        app.logger.exception("Error actualizando producto")
        return err_json("Error actualizando producto", 500, str(e))

@app.delete("/api/productos/<int:pid>")
def productos_delete(pid):
    p = Producto.query.get_or_404(pid)
    try:
        db.session.delete(p)
        db.session.commit()
        return ok_json({"ok": True}, 204)
    except Exception as e:
        db.session.rollback()
        app.logger.exception("Error eliminando producto")
        return err_json("Error eliminando producto", 500, str(e))

@app.post("/api/productos/<int:pid>/reponer")
def productos_reponer(pid):
    p = Producto.query.get_or_404(pid)
    litros = int(float((request.get_json(force=True) or {}).get("litros", 0)))
    if litros <= 0:
        return err_json("Litros inválidos", 400)
    p.cantidad = max(0, p.cantidad + litros)
    db.session.commit()
    return ok_json({"ok": True, "producto": {"id": p.id, "cantidad": p.cantidad, "slot": p.slot_id}})

@app.post("/api/productos/<int:pid>/reset_stock")
def productos_reset(pid):
    p = Producto.query.get_or_404(pid)
    litros = int(float((request.get_json(force=True) or {}).get("litros", 0)))
    if litros < 0:
        return err_json("Litros inválidos", 400)
    p.cantidad = litros
    db.session.commit()
    return ok_json({"ok": True, "producto": {"id": p.id, "cantidad": p.cantidad, "slot": p.slot_id}})

# -------------------------------------------------------------
# Pagos: listado
# -------------------------------------------------------------
@app.get("/api/pagos")
def pagos_list():
    try:
        limit = int(request.args.get("limit", 50))
        limit = max(1, min(limit, 200))
    except Exception:
        limit = 50

    estado = (request.args.get("estado") or "").strip()
    qsearch = (request.args.get("q") or "").strip()

    q = Pago.query
    if estado:
        q = q.filter(Pago.estado == estado)
    if qsearch:
        q = q.filter(Pago.mp_payment_id.ilike(f"%{qsearch}%"))

    pagos = q.order_by(Pago.id.desc()).limit(limit).all()
    return jsonify([
        {
            "id": p.id,
            "mp_payment_id": p.mp_payment_id,
            "estado": p.estado,
            "producto": p.producto,
            "product_id": p.product_id,
            "slot_id": p.slot_id,
            "litros": p.litros,
            "monto": p.monto,
            "dispensado": bool(p.dispensado),
            "created_at": p.created_at.isoformat() if p.created_at else None,
        } for p in pagos
    ])

# -------------------------------------------------------------
# Mercado Pago: crear preferencia
# -------------------------------------------------------------
@app.post("/api/pagos/preferencia")
def crear_preferencia():
    try:
        data = request.get_json(force=True, silent=True) or {}
        product_id = int(data.get("product_id") or 0)
        litros = int(data.get("litros") or 0)  # si no llega, usamos porción configurada
        if not MP_ACCESS_TOKEN:
            return err_json("MP_ACCESS_TOKEN faltante", 500)
        if not BACKEND_BASE_URL:
            return err_json("BACKEND_BASE_URL no configurado", 500)

        prod = Producto.query.get(product_id)
        if not prod or not prod.habilitado:
            return err_json("producto no disponible", 400)

        if litros <= 0:
            litros = int(prod.porcion_litros)

        total = float(prod.precio) * litros

        body = {
            "items": [{
                "id": str(prod.id),                        # respaldo de product_id
                "title": f"{prod.nombre} ({litros}L)",
                "category_id": str(prod.slot_id),          # respaldo de slot_id
                "quantity": 1,
                "currency_id": "ARS",
                "unit_price": total,                       # total (1 ítem)
            }],
            "metadata": {
                "slot_id": prod.slot_id,
                "product_id": prod.id,
                "producto": prod.nombre,
                "litros": litros,
            },
            "external_reference": f"{prod.id}|{prod.slot_id}|{litros}",
            "auto_return": "approved",
            "back_urls": {"success": WEB_URL, "failure": WEB_URL, "pending": WEB_URL},
            "notification_url": f"{BACKEND_BASE_URL}/api/mp/webhook",
            "statement_descriptor": "DISPEN-EASY",
        }

        app.logger.info(
            f"[MP] creando preferencia → notification_url={body['notification_url']} "
            f"meta={{product_id:{prod.id}, slot_id:{prod.slot_id}, litros:{litros}}}"
        )

        r = requests.post(
            "https://api.mercadopago.com/checkout/preferences",
            headers={"Authorization": f"Bearer {MP_ACCESS_TOKEN}", "Content-Type": "application/json"},
            json=body, timeout=20
        )
        try:
            r.raise_for_status()
        except Exception:
            app.logger.exception("[MP] error al crear preferencia: %s %s", r.status_code, r.text[:400])
            return err_json("mp_preference_failed", 502, r.text)

        pref = r.json() or {}
        link = pref.get("init_point") or pref.get("sandbox_init_point")
        if not link:
            return err_json("mp_response_without_link", 502, pref)
        return ok_json({"ok": True, "link": link, "raw": pref})
    except Exception as e:
        app.logger.exception("[MP] preferencia exception")
        return err_json("internal", 500, str(e))

# -------------------------------------------------------------
# Mercado Pago: Webhook (robusto + fallbacks)
# -------------------------------------------------------------
def _to_int(x, default=0):
    try:
        return int(x)
    except Exception:
        try:
            return int(float(x))
        except Exception:
            return default

@app.post("/api/mp/webhook")
def mp_webhook():
    body = request.get_json(silent=True) or {}
    args = request.args or {}

    topic = args.get("topic") or body.get("type")           # "payment" | "merchant_order"
    live_mode = bool(body.get("live_mode", True))
    base_api = "https://api.mercadopago.com" if live_mode else "https://api.sandbox.mercadopago.com"

    app.logger.info(f"[MP] webhook topic={topic} live_mode={live_mode} args={dict(args)}")
    app.logger.info(f"[MP] raw body={body}")

    payment_id = None
    merchant_order_id = None

    # --- 1) Notificación de payment
    if topic == "payment":
        if "resource" in body:  # formato viejo
            try:
                payment_id = (body["resource"].rstrip("/").split("/")[-1])
            except Exception:
                pass
        if not payment_id:
            payment_id = (body.get("data") or {}).get("id") or args.get("id")

    # --- 2) Notificación de merchant_order
    if topic == "merchant_order":
        merchant_order_id = args.get("id") or (body.get("data") or {}).get("id")

    # Si vino merchant_order, obtengo el/los payments asociados
    if topic == "merchant_order" and merchant_order_id:
        r_mo = requests.get(
            f"{base_api}/merchant_orders/{merchant_order_id}",
            headers={"Authorization": f"Bearer {MP_ACCESS_TOKEN}"},
            timeout=15
        )
        if r_mo.ok:
            pays = (r_mo.json() or {}).get("payments") or []
            if pays:
                payment_id = str(pays[0].get("id"))
        else:
            app.logger.error(f"[MP] MO {merchant_order_id} error {r_mo.status_code}: {r_mo.text[:300]}")

    if not payment_id:
        app.logger.warning("[MP] sin payment_id")
        return "ok", 200

    # --- Traer el pago (ojo: siempre usar MP_ACCESS_TOKEN)
    r_pay = requests.get(
        f"{base_api}/v1/payments/{payment_id}",
        headers={"Authorization": f"Bearer {MP_ACCESS_TOKEN}"},
        timeout=15
    )
    if not r_pay.ok:
        app.logger.error(f"[MP] error payment {payment_id}: {r_pay.status_code} {r_pay.text[:300]}")
        return "ok", 200

    pay = r_pay.json() or {}
    estado = (pay.get("status") or "").lower()  # approved/rejected/pending/…
    md = pay.get("metadata") or {}
    product_id = int(md.get("product_id") or 0)
    slot_id = int(md.get("slot_id") or 0)
    litros = int(md.get("litros") or 1)
    monto = int(round(float(pay.get("transaction_amount") or 0)))

    # --- UPSERT idempotente por mp_payment_id
    p = Pago.query.filter_by(mp_payment_id=str(payment_id)).first()
    if not p:
        p = Pago(
            mp_payment_id=str(payment_id),
            estado=estado,
            product_id=product_id,
            slot_id=slot_id,
            litros=litros,
            monto=monto,
            dispensado=False,
            raw=pay
        )
        db.session.add(p)
    else:
        p.estado = estado
        p.product_id = product_id
        p.slot_id = slot_id
        p.litros = litros
        p.monto = monto
        p.raw = pay

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        app.logger.exception("[DB] error guardando pago %s", payment_id)

    return "ok", 200

# Aliases por compatibilidad de rutas
@app.post("/webhook")
def mp_webhook_alias_root():
    return mp_webhook()

@app.post("/mp/webhook")
def mp_webhook_alias_mp():
    return mp_webhook()

# ----------------------- ESP32: pagos pendientes / confirmación -----

def _pago_to_dict(p):
    return {
        "id": p.id,
        "mp_payment_id": p.mp_payment_id,
        "estado": p.estado,
        "litros": int(p.litros or 0),
        "slot_id": int(p.slot_id or 0),
        "product_id": int(p.product_id or 0),
        "producto": getattr(p, "producto", None),   # por compat con versiones viejas
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }

@app.get("/api/pagos/pendiente")
def pagos_pendiente():
    """
    Devuelve el primer pago APROBADO que todavía no fue procesado ni dispensado.
    Sirve para que el ESP32 sepa qué y cuánto debe dispensar.
    """
    # Sólo pagos aprobados y todavía no tomados/dispensados
    p = (Pago.query
            .filter(Pago.estado.in_(["approved", "approved_partial"]),
                    (getattr(Pago, "procesado", False) == False) if hasattr(Pago, "procesado") else True,
                    Pago.dispensado == False)
            .order_by(Pago.created_at.asc())
            .first())

    if not p:
        return jsonify({"ok": True, "pago": None})

    # (opcional) marcar como "procesado" para que no lo tomen dos ESP a la vez
    if hasattr(p, "procesado"):
        p.procesado = True
        db.session.commit()

    # Traer datos del producto por si el firmware los necesita
    prod = Producto.query.get(p.product_id) if p.product_id else None
    data = _pago_to_dict(p)
    if prod:
        data["producto_nombre"] = prod.nombre
        data["porcion_litros"] = int(getattr(prod, "porcion_litros", 1))
        data["stock_litros"] = int(prod.cantidad)

    return jsonify({"ok": True, "pago": data})


@app.post("/api/pagos/<int:pid>/dispensado")
def pagos_dispensado(pid):
    """
    Lo llama el ESP32 cuando terminó de dispensar OK.
    Marca el pago como 'dispensado' y descuenta stock.
    """
    p = Pago.query.get_or_404(pid)
    if p.dispensado:
        return jsonify({"ok": True, "msg": "Ya estaba confirmado", "pago": _pago_to_dict(p)})

    # buscar producto y restar los litros efectivamente dispensados
    litros = int(p.litros or 0)
    prod = Producto.query.get(p.product_id) if p.product_id else None

    if prod and litros > 0:
        prod.cantidad = max(0, int(prod.cantidad) - litros)

    p.dispensado = True
    # si existe la columna 'procesado', mantenla en True
    if hasattr(p, "procesado"):
        p.procesado = True

    db.session.commit()
    resp = _pago_to_dict(p)
    if prod:
        resp["stock_restante"] = int(prod.cantidad)
        resp["producto_nombre"] = prod.nombre
    return jsonify({"ok": True, "msg": "Dispensado confirmado", "pago": resp})


@app.post("/api/pagos/<int:pid>/fallo")
def pagos_fallo(pid):
    """
    (Opcional, pero recomendado)
    Si el ESP32 intentó dispensar y falló (corte, sensor), libera el pago
    para reintento manual o para revisión.
    """
    p = Pago.query.get_or_404(pid)
    if hasattr(p, "procesado"):
        p.procesado = False   # lo volvemos a dejar disponible
    db.session.commit()
    return jsonify({"ok": True, "msg": "Pago liberado", "pago": _pago_to_dict(p)})
# -------------------------------------------------------------
# Run local
# -------------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
