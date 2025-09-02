# app.py
import os
import logging

import requests
from flask import Flask, jsonify, request
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
# -------------------------------------------------------------
# Config
# -------------------------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "").strip()
WEB_URL = os.getenv("WEB_URL", "https://example.com").strip()  # poné tu front si querés

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL or "sqlite:///local.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# CORS: permitir llamadas desde el front
CORS(app, resources={r"/api/*": {"origins": "*"}})

db = SQLAlchemy(app)

logging.basicConfig(level=logging.INFO)
app.logger.setLevel(logging.INFO)

# -------------------------------------------------------------
# Modelos (alineados a tu DB actual en Railway)
# -------------------------------------------------------------
class Producto(db.Model):
    __tablename__ = "producto"
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(100), nullable=False)
    precio = db.Column(db.Float, nullable=False)           # precio por litro
    cantidad = db.Column(db.Integer, nullable=False)       # stock en litros
    slot_id = db.Column(db.Integer, nullable=False, unique=True, index=True)
    created_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now(), nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), onupdate=db.func.now())
    habilitado = db.Column(db.Boolean, nullable=False, server_default=db.text("false"))

class Pago(db.Model):
    __tablename__ = "pago"
    id = db.Column(db.Integer, primary_key=True)
    mp_payment_id = db.Column(db.String(120), nullable=False, unique=True, index=True)
    estado = db.Column(db.String(80), nullable=False)
    producto = db.Column(db.String(120), nullable=False)      # nombre del producto (NOT NULL)
    dispensado = db.Column(db.Boolean, nullable=False, server_default=db.text("false"))
    slot_id = db.Column(db.Integer, nullable=True)
    monto = db.Column(db.Integer, nullable=True)              # ARS entero
    raw = db.Column(JSONB, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now(), nullable=False)
    product_id = db.Column(db.Integer, nullable=True)         # FK lógica
    procesado = db.Column(db.Boolean, nullable=False, server_default=db.text("false"))
    litros = db.Column(db.Integer, nullable=True)             # existe en tu DB

# ¡En producción NO crear/alterar! (dejamos comentado)
# with app.app_context():
#     db.create_all()

# -------------------------------------------------------------
# Helpers
# -------------------------------------------------------------
def require_token():
    if not ACCESS_TOKEN:
        app.logger.error("[MP] MP_ACCESS_TOKEN no configurado")
        return False
    return True

def json_error(message, status=400, extra=None):
    payload = {"error": message}
    if extra:
        payload.update(extra)
    return jsonify(payload), status

# -------------------------------------------------------------
# Rutas básicas
# -------------------------------------------------------------
@app.get("/")
def health():
    return jsonify({"status": "ok", "message": "Backend Dispen-Easy operativo"})

# ----------------------- Productos CRUD -----------------------
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
        # Unicidad del slot
        if Producto.query.filter(Producto.slot_id == p.slot_id).first():
            return json_error("Slot ya asignado a otro producto", 409)

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
            if new_slot != p.slot_id:
                if Producto.query.filter(Producto.slot_id == new_slot, Producto.id != p.id).first():
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

# ------------------- MercadoPago: preferencia -----------------
@app.post("/api/pagos/preferencia")
def crear_preferencia():
    data = request.get_json(force=True, silent=True) or {}
    product_id = int(data.get("product_id") or 0)
    litros = int(data.get("litros") or 1)

    prod = Producto.query.get(product_id)
    if not prod or not prod.habilitado:
        return jsonify({"error": "producto no disponible"}), 400

    if not BACKEND_BASE_URL:
        return jsonify({"error": "BACKEND_BASE_URL no configurado"}), 500

    body = {
        "items": [{
            "id": str(prod.id),
            "title": prod.nombre,
            "category_id": str(prod.slot_id),
            "quantity": 1,
            "currency_id": "ARS",
            "unit_price": float(prod.precio),
        }],
        "metadata": {
            "slot_id": prod.slot_id,
            "product_id": prod.id,
            "producto": prod.nombre,
            "litros": litros,
        },
        "external_reference": f"{prod.id}|{prod.slot_id}|{litros}",
        "auto_return": "approved",
        "back_urls": {
            "success": os.getenv("WEB_URL", "https://example.com"),
            "failure": os.getenv("WEB_URL", "https://example.com"),
            "pending": os.getenv("WEB_URL", "https://example.com"),
        },
        # 👇 clave: forzar https del backend público
        "notification_url": f"{BACKEND_BASE_URL}/api/mp/webhook",
        "statement_descriptor": "DISPEN-EASY",
    }

    app.logger.info(
        f"[MP] creando preferencia → notification_url={body['notification_url']} "
        f"meta={{'product_id': {prod.id}, 'slot_id': {prod.slot_id}, 'litros': {litros}}}"
    )

    r = requests.post(
        "https://api.mercadopago.com/checkout/preferences",
        headers={"Authorization": f"Bearer {ACCESS_TOKEN}", "Content-Type": "application/json"},
        json=body, timeout=20
    )
    try:
        r.raise_for_status()
    except Exception:
        app.logger.exception("[MP] error al crear preferencia: %s %s", r.status_code, r.text[:400])
        return jsonify({"error": "mp_preference_failed", "status": r.status_code, "body": r.text}), 500

    pref = r.json() or {}
    link = pref.get("init_point") or pref.get("sandbox_init_point")
    return jsonify({"ok": True, "link": link, "raw": pref})


# ----------------------- Webhook MP (robusto) ---------------------------
@app.post("/api/mp/webhook")
def mp_webhook():
    body = request.get_json(silent=True) or {}
    args = request.args or {}
    topic = args.get("topic") or body.get("type")  # payment / merchant_order
    live_mode = bool(body.get("live_mode", True))
    base_api = "https://api.mercadopago.com" if live_mode else "https://api.sandbox.mercadopago.com"

    app.logger.info(f"[MP] webhook topic={topic} live_mode={live_mode} args={dict(args)}")
    app.logger.info(f"[MP] raw body={body}")

    payment_id = None

    # 1) caso payment
    if topic == "payment":
        payment_id = (body.get("data") or {}).get("id") or args.get("id")
        if not payment_id and "resource" in body:  # formato viejo
            try:
                payment_id = body["resource"].rstrip("/").split("/")[-1]
            except Exception:
                pass

    # 2) caso merchant_order
    if topic == "merchant_order" and not payment_id:
        mo_id = args.get("id") or (body.get("data") or {}).get("id")
        if mo_id:
            r_mo = requests.get(f"{base_api}/merchant_orders/{mo_id}",
                                headers={"Authorization": f"Bearer {ACCESS_TOKEN}"}, timeout=15)
            if r_mo.ok:
                pays = (r_mo.json() or {}).get("payments") or []
                if pays:
                    payment_id = str(pays[0].get("id"))
            else:
                app.logger.error(f"[MP] MO {mo_id} error {r_mo.status_code}: {r_mo.text[:300]}")

    if not payment_id:
        app.logger.warning("[MP] sin payment_id")
        return "ok", 200

    # Traer el pago
    r_pay = requests.get(f"{base_api}/v1/payments/{payment_id}",
                         headers={"Authorization": f"Bearer {ACCESS_TOKEN}"}, timeout=15)
    if not r_pay.ok:
        app.logger.error(f"[MP] pay {payment_id} error {r_pay.status_code}: {r_pay.text[:300]}")
        return "ok", 200

    pay = r_pay.json() or {}
    app.logger.info(f"[MP] payment {payment_id} status={pay.get('status')} amount={pay.get('transaction_amount')}")

    # -------- extraer product_id / slot_id / litros con fallbacks ----------
    meta = pay.get("metadata") or {}
    addi = pay.get("additional_info") or {}
    items = addi.get("items") or []
    it0 = items[0] if items else {}
    ext = (pay.get("external_reference") or "").split("|")

    def _to_int(x, default=0):
        try:
            return int(x)
        except Exception:
            try:
                return int(float(x))
            except Exception:
                return default

    product_id = _to_int(meta.get("product_id") or it0.get("id") or (ext[0] if len(ext) > 0 else 0))
    slot_id    = _to_int(meta.get("slot_id")    or it0.get("category_id") or (ext[1] if len(ext) > 1 else 0))
    litros     = _to_int(meta.get("litros")     or (ext[2] if len(ext) > 2 else 0))
    producto   = (meta.get("producto")
                  or it0.get("title")
                  or pay.get("description")
                  or "")

    # Guardar/actualizar
    p = Pago.query.filter_by(mp_payment_id=str(payment_id)).first()
    if not p:
        p = Pago(
            mp_payment_id=str(payment_id),
            estado=pay.get("status") or "pending",
            producto=producto,
            dispensado=False,
            slot_id=slot_id,
            litros=litros,
            monto=_to_int(pay.get("transaction_amount") or 0),
            product_id=product_id,
            raw=pay,
        )
        db.session.add(p)
    else:
        p.estado = pay.get("status") or p.estado
        p.slot_id = slot_id or p.slot_id
        p.product_id = product_id or p.product_id
        p.litros = litros or p.litros
        p.producto = producto or p.producto
        p.monto = _to_int(pay.get("transaction_amount") or p.monto)
        p.raw = pay

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        app.logger.exception("[DB] error guardando pago %s", payment_id)

    return "ok", 200
#---------------Endpoint-------------------
# ----------------------- Pagos: listado ---------------------------
@app.get("/api/pagos")
def pagos_list():
    """
    Lista de pagos más recientes.
    Query params:
      - limit: cantidad a devolver (default 50, máx 200)
      - estado: filtrar por estado (ej: approved, pending, rejected)
      - q: búsqueda parcial por mp_payment_id
    """
    try:
        limit = int(request.args.get("limit", 50))
        limit = max(1, min(limit, 200))
    except Exception:
        limit = 50

    estado = (request.args.get("estado") or "").strip()
    q_search = (request.args.get("q") or "").strip()

    q = Pago.query
    if estado:
        q = q.filter(Pago.estado == estado)
    if q_search:
        # búsqueda simple por mp_payment_id contiene
        q = q.filter(Pago.mp_payment_id.ilike(f"%{q_search}%"))

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
            "procesado": bool(p.procesado),
            "created_at": p.created_at.isoformat() if p.created_at else None,
        }
        for p in pagos
    ])

#---------Ver Pagos--------

@app.get("/api/pagos")
def listar_pagos():
    pagos = Pago.query.order_by(Pago.id.desc()).limit(20).all()
    return jsonify([
        {
            "id": p.id,
            "mp_payment_id": p.mp_payment_id,
            "estado": p.estado,
            "litros": p.litros,
            "monto": p.monto,
            "slot_id": p.slot_id,
            "product_id": p.product_id,
            "created_at": p.created_at.isoformat() if p.created_at else None
        }
        for p in pagos
    ])

# -------------------------------------------------------------
# Run local
# -------------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
