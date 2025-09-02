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
    """
    Crea la preferencia de MP con redundancia para slot/product_id/litros:
    - metadata: { slot_id, product_id, litros, producto }
    - external_reference: "pid=<id>;slot=<slot>;litros=<n>"
    - items[0].id = product_id (fallback extra)
    """
    data = request.get_json(force=True, silent=True) or {}

    # Producto y litros solicitados
    product_id = int(data.get("product_id") or 0)
    litros_req = int(data.get("litros") or 0)

    prod = Producto.query.get(product_id)
    if not prod or not prod.habilitado:
        return jsonify({"error": "producto no disponible"}), 400

    # Porción por defecto = porcion_litros del producto, o 1
    litros = litros_req if litros_req > 0 else int(getattr(prod, "porcion_litros", 1) or 1)

    # Base URL del backend para armar notification_url (Railway, etc.)
    backend_base = os.getenv("BACKEND_BASE_URL") or request.url_root.rstrip("/")
    if not backend_base:
        return jsonify({"error": "BACKEND_BASE_URL no configurado"}), 500

    # Redundancia: external_reference para reconstruir si metadata no llega
    external_ref = f"pid={prod.id};slot={prod.slot_id};litros={litros}"

    body = {
        "items": [{
            "id": str(prod.id),              # fallback por si falta metadata
            "title": prod.nombre,
            "quantity": 1,
            "currency_id": "ARS",
            "unit_price": float(prod.precio),
        }],
        "metadata": {
            "slot_id": int(prod.slot_id),
            "product_id": int(prod.id),
            "producto": prod.nombre,
            "litros": int(litros),
        },
        "external_reference": external_ref,  # fallback adicional
        "auto_return": "approved",
        "back_urls": {
            "success": os.getenv("WEB_URL", "https://example.com"),
            "failure": os.getenv("WEB_URL", "https://example.com"),
            "pending": os.getenv("WEB_URL", "https://example.com"),
        },
        "notification_url": f"{backend_base}/api/mp/webhook",
        "statement_descriptor": "DISPEN-EASY",
    }

    app.logger.info(f"[MP] preferencia req → {body}")

    try:
        r = requests.post(
            "https://api.mercadopago.com/checkout/preferences",
            headers={
                "Authorization": f"Bearer {MP_ACCESS_TOKEN}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=20,
        )
        r.raise_for_status()
    except Exception as e:
        status = getattr(r, "status_code", 0)
        text = getattr(r, "text", "")
        app.logger.exception("[MP] error al crear preferencia: %s %s", status, text[:400])
        return jsonify({"error": "mp_preference_failed", "status": status, "detail": str(e), "body": text}), 500

    pref = r.json() or {}
    link = pref.get("init_point") or pref.get("sandbox_init_point")
    if not link:
        app.logger.error("[MP] preferencia creada sin init_point: %s", pref)
        return jsonify({"error": "preferencia_sin_link", "raw": pref}), 500

    return jsonify({"ok": True, "link": link, "raw": pref})
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
    """
    Webhook de MercadoPago:
    - Recibe notificación (topic=payment o merchant_order).
    - Pide al API oficial los detalles.
    - Guarda/actualiza pago en DB con product_id, slot_id y litros.
    - Evita duplicados con mp_payment_id único.
    """
    data = request.get_json(force=True, silent=True) or {}
    app.logger.info(f"[MP] webhook body={data}")

    # Extraer payment_id (según tipo de notificación)
    payment_id = None
    if "data" in data and isinstance(data["data"], dict):
        payment_id = data["data"].get("id")
    if not payment_id and "id" in data:
        payment_id = data.get("id")
    if not payment_id:
        app.logger.warning("[MP] webhook sin payment_id válido")
        return "no payment_id", 200

    # Confirmar datos desde MP
    try:
        r_pay = requests.get(
            f"{base_api}/v1/payments/{payment_id}",
            headers={"Authorization": f"Bearer {MP_ACCESS_TOKEN}"},
            timeout=15,
        )
        r_pay.raise_for_status()
    except Exception as e:
        app.logger.exception(f"[MP] error consultando payment {payment_id}: {e}")
        return "mp error", 200

    pay = r_pay.json() or {}
    estado = (pay.get("status") or "").lower()  # approved / rejected / pending
    raw_md = pay.get("metadata") or {}

    # --- Extraer metadata redundante ---
    product_id = int(raw_md.get("product_id") or 0)
    slot_id = int(raw_md.get("slot_id") or 0)
    litros = int(raw_md.get("litros") or 0)

    # Si no vino metadata, usar external_reference
    if (not product_id or not slot_id) and pay.get("external_reference"):
        try:
            parts = {
                kv.split("=")[0]: kv.split("=")[1]
                for kv in pay["external_reference"].split(";")
                if "=" in kv
            }
            product_id = int(parts.get("pid") or product_id or 0)
            slot_id = int(parts.get("slot") or slot_id or 0)
            litros = int(parts.get("litros") or litros or 0)
        except Exception:
            app.logger.warning(f"[MP] external_reference malformado: {pay['external_reference']}")

    monto = float(pay.get("transaction_amount") or 0)

    # --- Guardar en DB ---
    pago = Pago.query.filter_by(mp_payment_id=str(payment_id)).first()
    if not pago:
        pago = Pago(
            mp_payment_id=str(payment_id),
            estado=estado,
            product_id=product_id,
            slot_id=slot_id,
            litros=litros,
            monto=monto,
            dispensado=False,
            raw=pay,
        )
        db.session.add(pago)
    else:
        pago.estado = estado
        pago.product_id = product_id or pago.product_id
        pago.slot_id = slot_id or pago.slot_id
        pago.litros = litros or pago.litros
        pago.monto = monto or pago.monto
        pago.raw = pay

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        app.logger.exception(f"[MP] error guardando pago {payment_id}: {e}")
        return "db error", 500

    app.logger.info(f"[MP] pago {payment_id} → estado={estado}, pid={product_id}, slot={slot_id}, litros={litros}")
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
