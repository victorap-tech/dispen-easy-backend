# app.py – Dispenser Agua (2 productos, sin litros/stock en la UI)

import os
import logging
import threading
import json as _json
from typing import Optional

import requests
import paho.mqtt.client as mqtt
from flask import Flask, jsonify, request, make_response
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from sqlalchemy.dialects.postgresql import JSONB

# =========================
# Configuración básica
# =========================

DATABASE_URL = os.getenv("DATABASE_URL", "")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

BACKEND_BASE_URL = (os.getenv("BACKEND_BASE_URL", "") or "").rstrip("/")

MP_ACCESS_TOKEN_TEST = os.getenv("MP_ACCESS_TOKEN_TEST", "").strip()
MP_ACCESS_TOKEN_LIVE = os.getenv("MP_ACCESS_TOKEN_LIVE", "").strip()

MQTT_HOST = os.getenv("MQTT_HOST", "").strip()
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883") or 1883)
MQTT_USER = os.getenv("MQTT_USER", "")
MQTT_PASS = os.getenv("MQTT_PASS", "")

ADMIN_SECRET = os.getenv("ADMIN_SECRET", "").strip()

# =========================
# App + DB
# =========================

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL or "sqlite:///local.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

CORS(
    app,
    resources={r"/api/*": {"origins": "*"}},
    allow_headers=["Content-Type", "x-admin-secret"],
    expose_headers=["Content-Type"],
)

db = SQLAlchemy(app)
logging.basicConfig(level=logging.INFO)
app.logger.setLevel(logging.INFO)

# =========================
# Modelos
# =========================

class KV(db.Model):
    __tablename__ = "kv"
    key = db.Column(db.String(50), primary_key=True)
    value = db.Column(db.String(200), nullable=False)


class Dispenser(db.Model):
    __tablename__ = "dispenser"
    id = db.Column(db.Integer, primary_key=True)
    device_id = db.Column(db.String(80), nullable=False, unique=True, index=True)
    nombre = db.Column(db.String(100), nullable=False, default="")
    activo = db.Column(db.Boolean, nullable=False, server_default=db.text("true"))
    created_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now(), nullable=False)


class Producto(db.Model):
    __tablename__ = "producto"
    id = db.Column(db.Integer, primary_key=True)
    dispenser_id = db.Column(db.Integer, db.ForeignKey("dispenser.id", ondelete="SET NULL"), nullable=True, index=True)
    nombre = db.Column(db.String(100), nullable=False)
    precio = db.Column(db.Float, nullable=False)          # precio fijo por servicio
    cantidad = db.Column(db.Integer, nullable=False)      # no lo usamos (queda en 0)
    slot_id = db.Column(db.Integer, nullable=False)       # 1 ó 2
    porcion_litros = db.Column(db.Integer, nullable=False, server_default="1")
    bundle_precios = db.Column(JSONB, nullable=True)      # no lo usamos
    habilitado = db.Column(db.Boolean, nullable=False, server_default=db.text("true"))
    created_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now(), nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), onupdate=db.func.now())

    __table_args__ = (
        db.UniqueConstraint("dispenser_id", "slot_id", name="uq_disp_slot"),
    )


class Pago(db.Model):
    __tablename__ = "pago"
    id = db.Column(db.Integer, primary_key=True)
    mp_payment_id = db.Column(db.String(120), nullable=False, unique=True, index=True)
    estado = db.Column(db.String(80), nullable=False)
    producto = db.Column(db.String(120), nullable=False, default="")
    dispensado = db.Column(db.Boolean, nullable=False, server_default=db.text("false"))
    procesado = db.Column(db.Boolean, nullable=False, server_default=db.text("false"))
    slot_id = db.Column(db.Integer, nullable=False, default=0)
    litros = db.Column(db.Integer, nullable=False, default=1)   # para el ESP: siempre 1 “servicio”
    monto = db.Column(db.Integer, nullable=False, default=0)
    product_id = db.Column(db.Integer, nullable=False, default=0)
    dispenser_id = db.Column(db.Integer, nullable=False, default=0)
    device_id = db.Column(db.String(80), nullable=True, default="")
    raw = db.Column(JSONB, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now(), nullable=False)


# Crear tablas + valores por defecto
with app.app_context():
    db.create_all()

    # Modo MP por defecto
    if not KV.query.get("mp_mode"):
        db.session.add(KV(key="mp_mode", value="test"))
        db.session.commit()

    # Dispenser por defecto
    if Dispenser.query.count() == 0:
        d = Dispenser(device_id="dispen-01", nombre="dispen-01 (por defecto)", activo=True)
        db.session.add(d)
        db.session.commit()

# =========================
# Helpers
# =========================

def ok_json(data, status=200):
    return jsonify(data), status

def json_error(msg, status=400, extra=None):
    payload = {"error": msg}
    if extra is not None:
        payload["detail"] = extra
    return jsonify(payload), status

def _to_int(x, default=0):
    try:
        return int(x)
    except Exception:
        try:
            return int(float(x))
        except Exception:
            return default

def serialize_dispenser(d: Dispenser) -> dict:
    return {
        "id": d.id,
        "device_id": d.device_id,
        "nombre": d.nombre or "",
        "activo": bool(d.activo),
        "created_at": d.created_at.isoformat() if d.created_at else None,
    }

def serialize_producto(p: Producto) -> dict:
    return {
        "id": p.id,
        "dispenser_id": p.dispenser_id,
        "nombre": p.nombre,
        "precio": float(p.precio),
        "slot": int(p.slot_id),
        "habilitado": bool(p.habilitado),
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }

# =========================
# Auth simple para Admin
# =========================

PUBLIC_PATHS = {
    "/", "/gracias",
    "/api/config",
    "/api/pagos/preferencia",
    "/api/mp/webhook", "/webhook", "/mp/webhook",
}

@app.before_request
def _auth_guard():
    if request.method == "OPTIONS":
        return "", 200
    p = request.path
    if p in PUBLIC_PATHS:
        return None
    if not ADMIN_SECRET:
        # Sin secreto configurado, todo es público (modo dev)
        return None
    if request.headers.get("x-admin-secret") != ADMIN_SECRET:
        return json_error("unauthorized", 401)
    return None

# =========================
# Modo MercadoPago
# =========================

def get_mp_mode() -> str:
    row = KV.query.get("mp_mode")
    return (row.value if row else "test").lower()

def get_mp_token_and_base():
    mode = get_mp_mode()
    if mode == "live":
        return MP_ACCESS_TOKEN_LIVE, "https://api.mercadopago.com"
    return MP_ACCESS_TOKEN_TEST, "https://api.sandbox.mercadopago.com"

# =========================
# MQTT
# =========================

_mqtt_client: Optional[mqtt.Client] = None
_mqtt_lock = threading.Lock()

def topic_cmd(device_id: str) -> str:
    return f"dispen/{device_id}/cmd/dispense"

def _mqtt_on_connect(client, userdata, flags, rc, props=None):
    app.logger.info(f"[MQTT] conectado rc={rc}")

def _mqtt_on_message(client, userdata, msg):
    # Para este proyecto no necesitamos procesar mensajes entrantes
    app.logger.info(f"[MQTT] RX {msg.topic}: {msg.payload!r}")

def start_mqtt_background():
    if not MQTT_HOST:
        app.logger.warning("[MQTT] MQTT_HOST no configurado; no se inicia MQTT")
        return

    def _run():
        global _mqtt_client
        _mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="dispen-agua-backend")
        if MQTT_USER or MQTT_PASS:
            _mqtt_client.username_pw_set(MQTT_USER, MQTT_PASS)
        if MQTT_PORT == 8883:
            try:
                _mqtt_client.tls_set()
            except Exception as e:
                app.logger.error(f"[MQTT] TLS error: {e}")
        _mqtt_client.on_connect = _mqtt_on_connect
        _mqtt_client.on_message = _mqtt_on_message
        _mqtt_client.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
        _mqtt_client.loop_forever()

    threading.Thread(target=_run, name="mqtt-thread", daemon=True).start()

def send_dispense_cmd(device_id: str, payment_id: str, slot_id: int, litros: int = 1) -> bool:
    if not MQTT_HOST:
        return False
    payload = _json.dumps({
        "payment_id": str(payment_id),
        "slot_id": int(slot_id),
        "litros": int(litros or 1),   # el ESP lo interpreta como “porciones/tiempo”
    })
    with _mqtt_lock:
        if not _mqtt_client:
            return False
        t = topic_cmd(device_id)
        info = _mqtt_client.publish(t, payload, qos=1, retain=False)
        return info.rc == mqtt.MQTT_ERR_SUCCESS

# =========================
# Rutas básicas
# =========================

@app.get("/")
def health():
    return ok_json({"status": "ok", "mp_mode": get_mp_mode()})

@app.get("/api/config")
def api_config():
    return ok_json({"mp_mode": get_mp_mode()})

@app.post("/api/mp/mode")
def api_set_mp_mode():
    data = request.get_json(silent=True) or {}
    mode = str(data.get("mode") or "").lower()
    if mode not in ("test", "live"):
        return json_error("modo inválido (test|live)", 400)
    kv = KV.query.get("mp_mode") or KV(key="mp_mode", value=mode)
    kv.value = mode
    db.session.merge(kv)
    db.session.commit()
    return ok_json({"ok": True, "mp_mode": mode})

# ---------------- Dispensers ----------------

@app.get("/api/dispensers")
def api_dispensers_list():
    ds = Dispenser.query.order_by(Dispenser.id.asc()).all()
    return jsonify([serialize_dispenser(d) for d in ds])

@app.put("/api/dispensers/<int:did>")
def api_dispensers_update(did):
    d = Dispenser.query.get_or_404(did)
    data = request.get_json(silent=True) or {}
    if "nombre" in data:
        d.nombre = str(data["nombre"]).strip()
    if "activo" in data:
        d.activo = bool(data["activo"])
    if "device_id" in data:
        nid = str(data["device_id"]).strip()
        if nid and nid != d.device_id:
            if Dispenser.query.filter(Dispenser.device_id == nid, Dispenser.id != d.id).first():
                return json_error("device_id ya usado", 409)
            d.device_id = nid
    db.session.commit()
    return ok_json({"ok": True, "dispenser": serialize_dispenser(d)})

# ---------------- Productos (solo nombre + precio) ----------------

@app.get("/api/productos")
def api_productos_list():
    disp_id = _to_int(request.args.get("dispenser_id") or 0)
    q = Producto.query
    if disp_id:
        q = q.filter(Producto.dispenser_id == disp_id)
    prods = q.order_by(Producto.dispenser_id.asc(), Producto.slot_id.asc()).all()
    return jsonify([serialize_producto(p) for p in prods])

@app.post("/api/productos")
def api_productos_create():
    data = request.get_json(silent=True) or {}
    dispenser_id = _to_int(data.get("dispenser_id") or 0)
    if not dispenser_id or not Dispenser.query.get(dispenser_id):
        return json_error("dispenser_id inválido", 400)

    nombre = str(data.get("nombre") or "").strip()
    precio = float(data.get("precio") or 0)
    slot = _to_int(data.get("slot") or 0) or 1
    habilitado = bool(data.get("habilitado", True))

    if not nombre:
        return json_error("nombre requerido", 400)
    if precio <= 0:
        return json_error("precio debe ser > 0", 400)

    if Producto.query.filter(Producto.dispenser_id == dispenser_id,
                             Producto.slot_id == slot).first():
        return json_error("slot ya usado en este dispenser", 409)

    p = Producto(
        dispenser_id=dispenser_id,
        nombre=nombre,
        precio=precio,
        cantidad=0,
        slot_id=slot,
        porcion_litros=1,
        bundle_precios={},
        habilitado=habilitado,
    )
    db.session.add(p)
    db.session.commit()
    return ok_json({"ok": True, "producto": serialize_producto(p)}, 201)

@app.put("/api/productos/<int:pid>")
def api_productos_update(pid):
    p = Producto.query.get_or_404(pid)
    data = request.get_json(silent=True) or {}
    try:
        if "nombre" in data:
            p.nombre = str(data["nombre"]).strip()
        if "precio" in data:
            precio = float(data["precio"])
            if precio <= 0:
                return json_error("precio debe ser > 0", 400)
            p.precio = precio
        if "habilitado" in data:
            p.habilitado = bool(data["habilitado"])
        if "slot" in data:
            new_slot = _to_int(data["slot"])
            if new_slot != p.slot_id:
                if Producto.query.filter(
                    Producto.dispenser_id == p.dispenser_id,
                    Producto.slot_id == new_slot,
                    Producto.id != p.id,
                ).first():
                    return json_error("slot ya usado en este dispenser", 409)
                p.slot_id = new_slot
        db.session.commit()
        return ok_json({"ok": True, "producto": serialize_producto(p)})
    except Exception as e:
        db.session.rollback()
        return json_error("error actualizando producto", 500, str(e))

# ---------------- Pagos (historial + reenviar) ----------------

@app.get("/api/pagos")
def api_pagos_list():
    try:
        limit = int(request.args.get("limit", 50))
        limit = max(1, min(limit, 200))
    except Exception:
        limit = 50

    q = Pago.query.order_by(Pago.id.desc()).limit(limit)
    pagos = q.all()
    return jsonify([
        {
            "id": p.id,
            "mp_payment_id": p.mp_payment_id,
            "estado": p.estado,
            "producto": p.producto,
            "product_id": p.product_id,
            "dispenser_id": p.dispenser_id,
            "device_id": p.device_id,
            "slot_id": p.slot_id,
            "litros": p.litros,
            "monto": p.monto,
            "dispensado": bool(p.dispensado),
            "created_at": p.created_at.isoformat() if p.created_at else None,
        }
        for p in pagos
    ])

@app.post("/api/pagos/<int:pid>/reenviar")
def api_pagos_reenviar(pid):
    p = Pago.query.get_or_404(pid)
    if p.estado != "approved":
        return json_error("solo pagos approved se pueden reenviar", 400)
    if not p.slot_id or not (p.device_id or p.dispenser_id):
        return json_error("pago sin slot o dispenser", 400)

    device = p.device_id
    if not device and p.product_id:
        prod = Producto.query.get(p.product_id)
        if prod and prod.dispenser_id:
            d = Dispenser.query.get(prod.dispenser_id)
            device = d.device_id if d else ""

    if not device:
        return json_error("sin device_id asociado", 400)

    published = send_dispense_cmd(device, p.mp_payment_id, p.slot_id, p.litros or 1)
    if not published:
        return json_error("no se pudo publicar MQTT", 500)

    return ok_json({"ok": True, "msg": "comando reenviado por MQTT"})

# ---------------- Opciones de producto (para generar QR) ----------------
@app.get("/api/productos/<int:pid>/opciones")
def api_productos_opciones(pid):
    prod = Producto.query.get_or_404(pid)
    disp = Dispenser.query.get(prod.dispenser_id) if prod.dispenser_id else None

    # Si el producto o el dispenser están deshabilitados
    if not prod.habilitado:
        return json_error("no_disponible", 400)
    if not disp or not disp.activo:
        return json_error("dispenser no disponible", 400)

    # Modelo simple: solo 1 “litro” = 1 servicio
    options = [{
        "litros": 1,
        "disponible": True,
        "precio_final": int(round(float(prod.precio)))
    }]

    return ok_json({
        "ok": True,
        "producto": serialize_producto(prod),
        "opciones": options
    })
# ---------------- Crear preferencia MP ----------------

@app.post("/api/pagos/preferencia")
def api_crear_preferencia():
    data = request.get_json(silent=True) or {}
    product_id = _to_int(data.get("product_id") or 0)

    token, base_api = get_mp_token_and_base()
    if not token:
        return json_error("MP token no configurado", 500)

    prod = Producto.query.get(product_id)
    if not prod or not prod.habilitado:
        return json_error("producto no disponible", 400)

    disp = Dispenser.query.get(prod.dispenser_id) if prod.dispenser_id else None
    if not disp or not disp.activo:
        return json_error("dispenser no disponible", 400)

    monto_final = int(round(float(prod.precio)))
    backend_base = BACKEND_BASE_URL or request.url_root.rstrip("/")

    external_ref = f"pid={prod.id};slot={prod.slot_id};disp={disp.id};dev={disp.device_id}"
    body = {
        "items": [{
            "id": str(prod.id),
            "title": prod.nombre,
            "description": prod.nombre,
            "quantity": 1,
            "currency_id": "ARS",
            "unit_price": float(monto_final),
        }],
        "description": prod.nombre,
        "metadata": {
            "slot_id": int(prod.slot_id),
            "product_id": int(prod.id),
            "producto": prod.nombre,
            "litros": 1,
            "dispenser_id": int(disp.id),
            "device_id": disp.device_id,
            "precio_final": int(monto_final),
        },
        "external_reference": external_ref,
        "auto_return": "approved",
        "back_urls": {
            "success": f"{backend_base}/gracias?status=success",
            "failure": f"{backend_base}/gracias?status=failure",
            "pending": f"{backend_base}/gracias?status=pending",
        },
        "notification_url": f"{backend_base}/api/mp/webhook",
        "statement_descriptor": "DISPEN-AGUA",
    }

    try:
        r = requests.post(
            f"{base_api}/checkout/preferences",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=20,
        )
        r.raise_for_status()
    except Exception as e:
        detail = getattr(e, "response", None)
        detail_txt = getattr(detail, "text", str(e))[:600]
        return json_error("mp_preference_failed", 502, detail_txt)

    pref = r.json() or {}
    link = pref.get("init_point") or pref.get("sandbox_init_point")
    if not link:
        return json_error("preferencia_sin_link", 502, pref)

    return ok_json({"ok": True, "link": link, "precio_final": monto_final})

# ---------------- Webhook MercadoPago ----------------

@app.post("/api/mp/webhook")
def mp_webhook():
    body = request.get_json(silent=True) or {}
    args = request.args or {}
    topic = args.get("topic") or body.get("type")
    live_mode = bool(body.get("live_mode", True))

    base_api = "https://api.mercadopago.com" if live_mode else "https://api.sandbox.mercadopago.com"
    token, _ = get_mp_token_and_base()
    if not token:
        return "ok", 200

    payment_id = None
    if topic == "payment":
        if isinstance(body.get("resource"), str):
            try:
                payment_id = body["resource"].rstrip("/").split("/")[-1]
            except Exception:
                payment_id = None
        payment_id = payment_id or (body.get("data") or {}).get("id") or args.get("id")

    if not payment_id:
        return "ok", 200

    try:
        r_pay = requests.get(
            f"{base_api}/v1/payments/{payment_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        r_pay.raise_for_status()
    except Exception:
        return "ok", 200

    pay = r_pay.json() or {}
    estado = (pay.get("status") or "").lower()
    md = pay.get("metadata") or {}

    product_id = _to_int(md.get("product_id") or 0)
    slot_id = _to_int(md.get("slot_id") or 0)
    litros = _to_int(md.get("litros") or 1)
    dispenser_id = _to_int(md.get("dispenser_id") or 0)
    device_id = str(md.get("device_id") or "")
    monto = _to_int(md.get("precio_final") or 0) or _to_int(pay.get("transaction_amount") or 0)
    producto_txt = (md.get("producto") or pay.get("description") or "")[:120]

    p = Pago.query.filter_by(mp_payment_id=str(payment_id)).first()
    if not p:
        p = Pago(
            mp_payment_id=str(payment_id),
            estado=estado or "pending",
            producto=producto_txt,
            dispensado=False,
            procesado=False,
            slot_id=slot_id,
            litros=litros or 1,
            monto=monto,
            product_id=product_id,
            dispenser_id=dispenser_id,
            device_id=device_id,
            raw=pay,
        )
        db.session.add(p)
    else:
        p.estado = estado or p.estado
        p.producto = producto_txt or p.producto
        p.slot_id = p.slot_id or slot_id
        p.product_id = p.product_id or product_id
        p.litros = p.litros or (litros or 1)
        p.monto = p.monto or monto
        p.dispenser_id = p.dispenser_id or dispenser_id
        p.device_id = p.device_id or device_id
        p.raw = pay

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return "ok", 200

    # Enviar comando al ESP si está approved
    try:
        if p.estado == "approved" and not p.dispensado and not p.procesado and p.slot_id:
            dev = p.device_id
            if not dev and p.product_id:
                pr = Producto.query.get(p.product_id)
                if pr and pr.dispenser_id:
                    d = Dispenser.query.get(pr.dispenser_id)
                    dev = d.device_id if d else ""

            if dev:
                published = send_dispense_cmd(dev, p.mp_payment_id, p.slot_id, p.litros or 1)
                if published:
                    p.procesado = True
                    db.session.commit()
    except Exception:
        pass

    return "ok", 200

@app.post("/webhook")
def mp_webhook_alias1():
    return mp_webhook()

@app.post("/mp/webhook")
def mp_webhook_alias2():
    return mp_webhook()

# ---------------- Página de gracias simple ----------------

@app.get("/gracias")
def pagina_gracias():
    status = (request.args.get("status") or "").lower()
    if status in ("success", "approved"):
        title = "¡Gracias por su compra!"
        msg = "<p>El pago fue aprobado. El dispenser debería activarse en unos segundos.</p>"
    elif status in ("pending", "in_process"):
        title = "Pago pendiente"
        msg = "<p>Tu pago está en revisión. Si se aprueba, se activará el dispenser automáticamente.</p>"
    else:
        title = "Pago no completado"
        msg = "<p>El pago fue cancelado o rechazado.</p>"

    html = f"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{title}</title>
</head>
<body style="background:#0b1220;color:#e5e7eb;font-family:Inter,system-ui,Segoe UI,Roboto">
<div style="max-width:720px;margin:16vh auto;background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.08);border-radius:16px;padding:20px">
<h1 style="margin:0 0 8px">{title}</h1>
{msg}
</div>
</body>
</html>"""
    r = make_response(html, 200)
    r.headers["Content-Type"] = "text/html; charset=utf-8"
    return r

# =========================
# Inicializar MQTT
# =========================

with app.app_context():
    try:
        start_mqtt_background()
    except Exception:
        app.logger.exception("[MQTT] Error iniciando hilo MQTT")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
