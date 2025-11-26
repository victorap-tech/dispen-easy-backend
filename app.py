# app.py – Dispenser Agua (2 productos, con tiempo_segundos configurable)

import os
import logging
import threading
import json as _json
from typing import Optional

import requests
import paho.mqtt.client as mqtt
import mercadopago
import json
from flask import Flask, jsonify, request, make_response, redirect
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
WEB_URL = os.getenv("WEB_URL", BACKEND_BASE_URL)

MP_ACCESS_TOKEN_TEST = os.getenv("MP_ACCESS_TOKEN_TEST", "").strip()
MP_ACCESS_TOKEN_LIVE = os.getenv("MP_ACCESS_TOKEN_LIVE", "").strip()

MP_CLIENT_ID = os.getenv("MP_CLIENT_ID", "").strip()
MP_CLIENT_SECRET = os.getenv("MP_CLIENT_SECRET", "").strip()

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
    precio = db.Column(db.Float, nullable=False)
    cantidad = db.Column(db.Integer, nullable=False)
    slot_id = db.Column(db.Integer, nullable=False)
    porcion_litros = db.Column(db.Integer, nullable=False, server_default="1")
    bundle_precios = db.Column(JSONB, nullable=True)
    habilitado = db.Column(db.Boolean, nullable=False, server_default=db.text("true"))

    # *** NUEVO ***
    tiempo_segundos = db.Column(db.Integer, nullable=False, default=23)

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
    litros = db.Column(db.Integer, nullable=False, default=1)
    monto = db.Column(db.Integer, nullable=False, default=0)
    product_id = db.Column(db.Integer, nullable=False, default=0)
    dispenser_id = db.Column(db.Integer, nullable=False, default=0)
    device_id = db.Column(db.String(80), nullable=True, default="")
    raw = db.Column(JSONB, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now(), nullable=False)

# -----------------------
# PROCESAR PAGO
# -----------------------

def _procesar_pago_desde_info(payment_id: str, info: dict):
    status_raw = info.get("status")
    status = str(status_raw).lower() if status_raw is not None else ""
    metadata = info.get("metadata") or {}

    app.logger.info(f"[WEBHOOK] payment_id={payment_id} status={status} metadata={metadata}")

    # Recuperar metadata si no vino
    if not metadata:
        try:
            pref_id = info.get("order", {}).get("id") or info.get("preference_id")
            if pref_id:
                token, _ = get_mp_token_and_base()
                r2 = requests.get(
                    f"https://api.mercadopago.com/checkout/preferences/{pref_id}",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=10
                )
                r2.raise_for_status()
                metadata = (r2.json() or {}).get("metadata") or {}
                app.logger.info("[WEBHOOK] Metadata recuperada desde preferencia")
        except Exception as e:
            app.logger.error(f"[WEBHOOK] No se pudo recuperar metadata: {e}")

    product_id   = _to_int(metadata.get("product_id") or 0)
    slot_id      = _to_int(metadata.get("slot_id") or 0)
    dispenser_id = _to_int(metadata.get("dispenser_id") or 0)
    device_id    = (metadata.get("device_id") or "").strip()
    litros_md    = _to_int(metadata.get("litros") or 1)
    tiempo_seg   = _to_int(metadata.get("tiempo_segundos") or 23)
    producto_nom = metadata.get("producto") or info.get("description") or ""
    monto_val    = info.get("transaction_amount") or metadata.get("precio_final") or 0

    if not device_id or not slot_id:
        return

    pago = Pago.query.filter_by(mp_payment_id=str(payment_id)).first()

    if pago and pago.procesado and status == "approved":
        return

    if not pago:
        pago = Pago(
            mp_payment_id=str(payment_id),
            estado=status,
            producto=producto_nom,
            procesado=False,
            slot_id=slot_id,
            litros=litros_md,
            product_id=product_id,
            dispenser_id=dispenser_id,
            device_id=device_id,
            monto=monto_val,
            raw=info
        )
        db.session.add(pago)
    else:
        pago.estado = status
        pago.producto = producto_nom or pago.producto
        pago.slot_id = slot_id or pago.slot_id
        pago.litros = litros_md or pago.litros
        pago.monto = monto_val or pago.monto
        pago.raw = info

    db.session.commit()

    if status == "approved" and not pago.procesado:
        ok = send_dispense_cmd(device_id, payment_id, slot_id, litros_md, tiempo_seg)
        if ok:
            pago.procesado = True
            db.session.commit()


# -----------------------
# Helpers
# -----------------------

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
    except:
        try:
            return int(float(x))
        except:
            return default

def serialize_dispenser(d: Dispenser) -> dict:
    return {
        "id": d.id,
        "device_id": d.device_id,
        "nombre": d.nombre,
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
        "tiempo_segundos": int(p.tiempo_segundos or 23),  # *** NUEVO ***
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }

def kv_set(key, value):
    row = KV.query.get(key)
    if row:
        row.value = value
    else:
        row = KV(key=key, value=value)
        db.session.add(row)
    db.session.commit()

def kv_get(key, default=""):
    row = KV.query.get(key)
    return row.value if row else default


# -----------------------
# Auth simple Admin
# -----------------------

PUBLIC_PATHS = {
    "/", "/gracias",
    "/api/config",
    "/api/pagos/preferencia",
    "/api/mp/webhook", "/webhook", "/mp/webhook",
    "/api/mp/oauth/init",
    "/api/mp/oauth/callback",
}

@app.before_request
def _auth_guard():
    if request.method == "OPTIONS":
        return "", 200

    p = request.path

    if p in PUBLIC_PATHS or p.startswith("/qr/"):
        return None

    if not ADMIN_SECRET:
        return None

    if request.headers.get("x-admin-secret") != ADMIN_SECRET:
        return json_error("unauthorized", 401)

    return None
    # -----------------------
# MP: Tokens
# -----------------------

def get_mp_mode() -> str:
    row = KV.query.get("mp_mode")
    return (row.value if row else "test").lower()

def get_oauth_access_token() -> str:
    return kv_get("mp_oauth_access_token", "").strip()

def get_mp_token_and_base():
    oauth = get_oauth_access_token()
    if oauth:
        return oauth, "https://api.mercadopago.com"

    mode = get_mp_mode()
    if mode == "live":
        return MP_ACCESS_TOKEN_LIVE, "https://api.mercadopago.com"

    return MP_ACCESS_TOKEN_TEST, "https://api.mercadopago.com"


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
    app.logger.info(f"[MQTT RX] {msg.topic}: {msg.payload!r}")

def start_mqtt_background():
    if not MQTT_HOST:
        app.logger.warning("[MQTT] MQTT_HOST no configurado; no se inicia MQTT")
        return

    def _run():
        global _mqtt_client
        _mqtt_client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id="dispen-agua-backend"
        )
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

def send_dispense_cmd(
    device_id: str,
    payment_id: str,
    slot_id: int,
    litros: int = 1,
    tiempo_segundos: int = 23
) -> bool:
    """
    Publica en MQTT el comando de dispensado.
    Ahora incluye tiempo_segundos para que el ESP32 lo use.
    """
    if not MQTT_HOST:
        return False

    payload = _json.dumps({
        "payment_id": str(payment_id),
        "slot_id": int(slot_id),
        "litros": int(litros or 1),
        "tiempo_segundos": int(tiempo_segundos or 23),
    })

    for i in range(10):  # reintenta hasta 10 veces
        with _mqtt_lock:
            if _mqtt_client:
                t = topic_cmd(device_id)
                info = _mqtt_client.publish(t, payload, qos=1, retain=False)
                if info.rc == mqtt.MQTT_ERR_SUCCESS:
                    app.logger.info(f"[MQTT] Publicado OK → {t} {payload}")
                    return True
                else:
                    app.logger.error(f"[MQTT] ERROR rc={info.rc}")

        app.logger.info("[MQTT] Cliente no listo, reintentando...")
        import time
        time.sleep(0.5)

    app.logger.error("[MQTT] No se pudo publicar comando después de reintentos")
    return False


# =========================
# RUTAS BÁSICAS
# =========================

@app.get("/")
def health():
    return ok_json({
        "status": "ok",
        "mp_mode": get_mp_mode(),
        "oauth_linked": bool(get_oauth_access_token()),
    })

@app.get("/api/config")
def api_config():
    return ok_json({
        "mp_mode": get_mp_mode(),
        "oauth_linked": bool(get_oauth_access_token()),
    })

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


# =========================
# DISPENSERS
# =========================

@app.get("/api/dispensers")
def api_dispensers_list():
    ds = Dispenser.query.order_by(Dispenser.id.asc()).all()
    return jsonify([serialize_dispenser(d) for d in ds])

@app.post("/api/dispensers")
def api_dispensers_create():
    try:
        data = request.get_json(silent=True) or {}
        name = str(data.get("nombre") or "").strip()

        if not name:
            all_d = Dispenser.query.order_by(Dispenser.id.asc()).all()
            next_num = len(all_d) + 1
            name = f"dispen-{next_num:02d}"

        device_id = name

        d = Dispenser(
            device_id=device_id,
            nombre=name,
            activo=True
        )
        db.session.add(d)
        db.session.commit()

        # Por defecto 23 segundos en ambos slots
        p1 = Producto(
            dispenser_id=d.id,
            nombre="Agua fría",
            precio=0,
            cantidad=0,
            slot_id=1,
            habilitado=False,
            tiempo_segundos=23,
        )
        p2 = Producto(
            dispenser_id=d.id,
            nombre="Agua caliente",
            precio=0,
            cantidad=0,
            slot_id=2,
            habilitado=False,
            tiempo_segundos=23,
        )
        db.session.add(p1)
        db.session.add(p2)
        db.session.commit()

        return ok_json({
            "ok": True,
            "dispenser": serialize_dispenser(d),
            "productos": [
                serialize_producto(p1),
                serialize_producto(p2)
            ]
        }, 201)

    except Exception as e:
        db.session.rollback()
        return json_error("error creando dispenser", 500, str(e))


# =========================
# PRODUCTOS
# =========================

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
    tiempo_seg = _to_int(data.get("tiempo_segundos") or 23)

    if not nombre:
        return json_error("nombre requerido", 400)
    if precio <= 0:
        return json_error("precio debe ser > 0", 400)
    if tiempo_seg <= 0:
        return json_error("tiempo_segundos debe ser > 0", 400)

    if Producto.query.filter(
        Producto.dispenser_id == dispenser_id,
        Producto.slot_id == slot
    ).first():
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
        tiempo_segundos=tiempo_seg,
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
        if "tiempo_segundos" in data:
            t = _to_int(data["tiempo_segundos"])
            if t <= 0:
                return json_error("tiempo_segundos debe ser > 0", 400)
            p.tiempo_segundos = t

        db.session.commit()
        return ok_json({"ok": True, "producto": serialize_producto(p)})
    except Exception as e:
        db.session.rollback()
        return json_error("error actualizando producto", 500, str(e))


# =========================
# PAGOS – HISTORIAL
# =========================

@app.get("/api/pagos")
def api_pagos_list():
    try:
        limit = int(request.args.get("limit", 50))
        limit = max(1, min(limit, 200))
    except:
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
            "procesado": bool(p.procesado),
            "created_at": p.created_at.isoformat() if p.created_at else None,
        }
        for p in pagos
    ])

@app.post("/api/pagos/<int:pid>/reenviar")
def api_pagos_reenviar(pid):
    p = Pago.query.get_or_404(pid)

    if p.estado != "approved":
        return json_error("solo pagos approved", 400)
    if not p.slot_id:
        return json_error("pago sin slot", 400)

    device = p.device_id
    tiempo_seg = 23

    # Recuperar tiempo desde el producto si es posible
    if p.product_id:
        prod = Producto.query.get(p.product_id)
        if prod:
            tiempo_seg = int(prod.tiempo_segundos or 23)
            if prod.dispenser_id and not device:
                d = Dispenser.query.get(prod.dispenser_id)
                if d:
                    device = d.device_id

    if not device:
        return json_error("sin device_id", 400)

    ok = send_dispense_cmd(device, p.mp_payment_id, p.slot_id, p.litros or 1, tiempo_seg)
    if not ok:
        return json_error("no se pudo publicar MQTT", 500)

    return ok_json({"ok": True, "msg": "comando reenviado"})


# =========================
# HELPERS BACKEND BASE
# =========================

def get_backend_base() -> str:
    """
    Devuelve la URL base del backend.
    Usa BACKEND_BASE_URL si está seteada, si no usa request.url_root.
    Siempre sin / final.
    """
    base = (BACKEND_BASE_URL or "").strip()
    if not base:
        base = (request.url_root or "").rstrip("/")
    return base


# =========================
# PAGOS – PREFERENCIA (ADMIN)
# =========================

@app.post("/api/pagos/preferencia")
def crear_preferencia_api():
    import time
    data = request.get_json(force=True, silent=True) or {}
    product_id = _to_int(data.get("product_id") or 0)

    token, _base_api = get_mp_token_and_base()
    if not token:
        return json_error("MP token no configurado", 500)

    prod = Producto.query.get(product_id)
    if not prod or not prod.habilitado:
        return json_error("producto no disponible", 400)

    disp = Dispenser.query.get(prod.dispenser_id)
    if not disp or not disp.activo:
        return json_error("dispenser no disponible", 400)

    monto_final = int(prod.precio)
    ts = int(time.time())
    tiempo_seg = int(prod.tiempo_segundos or 23)

    external_reference = (
        f"product_id={prod.id};slot={prod.slot_id};disp={disp.id};dev={disp.device_id};ts={ts}"
    )

    backend_url = get_backend_base()

    body = {
        "items": [{
            "id": str(prod.id),
            "title": prod.nombre,
            "description": prod.nombre,
            "quantity": 1,
            "currency_id": "ARS",
            "unit_price": float(monto_final),
        }],

        "metadata": {
            "product_id": prod.id,
            "slot_id": prod.slot_id,
            "producto": prod.nombre,
            "litros": 1,
            "dispenser_id": disp.id,
            "device_id": disp.device_id,
            "precio_final": monto_final,
            "tiempo_segundos": tiempo_seg,
        },

        "external_reference": external_reference,
        "auto_return": "approved",

        "back_urls": {
            "success": f"{backend_url}/gracias",
            "failure": f"{backend_url}/gracias",
            "pending": f"{backend_url}/gracias"
        },

        "notification_url": f"{backend_url}/api/mp/webhook",
        "purpose": "wallet_purchase",
        "expires": False,

        "payment_methods": {
            "excluded_payment_types": [],
            "installments": 1,
            "default_payment_method_id": None
        },

        "binary_mode": False,
        "statement_descriptor": "DISPENSER-AGUA"
    }

    try:
        r = requests.post(
            "https://api.mercadopago.com/checkout/preferences",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            },
            json=body,
            timeout=20
        )
        r.raise_for_status()
    except Exception as e:
        detail = getattr(r, "text", str(e))[:600]
        return json_error("mp_preference_failed", 502, detail)

    pref = r.json() or {}
    link = pref.get("init_point") or pref.get("sandbox_init_point")
    if not link:
        return json_error("preferencia_sin_link", 502, pref)

    return ok_json({"ok": True, "link": link})


# =========================
# QR UNIVERSAL POR DISPENSER Y SLOT
# =========================

@app.get("/qr/<device_id>/<int:slot_id>")
def qr_universal(device_id, slot_id):
    """
    QR físico: https://TU_DOMINIO/qr/<device_id>/<slot_id>
    Siempre crea una nueva preferencia para ese dispenser+slot.
    """

    disp = Dispenser.query.filter_by(device_id=device_id).first()
    if not disp or not disp.activo:
        return "Dispenser no disponible", 404

    prod = Producto.query.filter_by(
        dispenser_id=disp.id,
        slot_id=slot_id
    ).first()

    if not prod or not prod.habilitado:
        return "Producto no disponible", 404

    token, _base_api = get_mp_token_and_base()
    if not token:
        return "MP token no configurado", 500

    backend_url = get_backend_base()
    monto_final = float(prod.precio)
    tiempo_seg = int(prod.tiempo_segundos or 23)

    body = {
        "items": [{
            "id": str(prod.id),
            "title": prod.nombre,
            "description": prod.nombre,
            "quantity": 1,
            "currency_id": "ARS",
            "unit_price": monto_final,
        }],

        "metadata": {
            "product_id": prod.id,
            "slot_id": prod.slot_id,
            "producto": prod.nombre,
            "litros": 1,
            "dispenser_id": disp.id,
            "device_id": disp.device_id,
            "precio_final": monto_final,
            "tiempo_segundos": tiempo_seg,
        },

        "notification_url": f"{backend_url}/api/mp/webhook",
        "auto_return": "approved",
        "back_urls": {
            "success": f"{backend_url}/gracias",
            "failure": f"{backend_url}/gracias",
            "pending": f"{backend_url}/gracias"
        },

        "purpose": "wallet_purchase",
        "expires": False,
        "binary_mode": False,
        "statement_descriptor": "DISPENSER-AGUA"
    }

    try:
        r = requests.post(
            "https://api.mercadopago.com/checkout/preferences",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            },
            json=body,
            timeout=20
        )
        r.raise_for_status()
    except Exception as e:
        return f"Error al crear preferencia: {e}", 500

    pref = r.json() or {}
    link = pref.get("init_point") or pref.get("sandbox_init_point")
    if not link:
        return "Preferencia sin link", 502

    return redirect(link)


# =========================
# WEBHOOK MP (payment + merchant_order)
# =========================

@app.post("/api/mp/webhook")
def mp_webhook():
    try:
        data = request.json or {}
        app.logger.info(f"[WEBHOOK] recibido: {data}")

        tipo = (
            data.get("type") or
            data.get("topic") or
            data.get("action") or ""
        ).lower()

        if not ("payment" in tipo or "merchant_order" in tipo):
            return "ok", 200

        token, _ = get_mp_token_and_base()
        mp_sdk = mercadopago.SDK(token)

        # ---- PAYMENT ----
        if "payment" in tipo:
            payment_id = None

            if isinstance(data.get("data"), dict):
                payment_id = data["data"].get("id")

            if not payment_id and data.get("resource"):
                payment_id = str(data["resource"]).split("/")[-1]

            if not payment_id:
                return "ok", 200

            payment_id = str(payment_id)

            try:
                resp = mp_sdk.payment().get(payment_id)
                info = resp.get("response") or {}
            except:
                return "ok", 200

            _procesar_pago_desde_info(payment_id, info)

        # ---- MERCHANT ORDER ----
        if "merchant_order" in tipo:
            mo_id = None

            if isinstance(data.get("data"), dict):
                mo_id = data["data"].get("id")

            if not mo_id and data.get("resource"):
                mo_id = str(data["resource"]).split("/")[-1]

            if not mo_id:
                return "ok", 200

            mo_id = str(mo_id)

            try:
                mo_resp = mp_sdk.merchant_order().get(mo_id)
                mo_info = mo_resp.get("response") or {}
            except:
                return "ok", 200

            for pay in mo_info.get("payments") or []:
                p_id = pay.get("id")
                if not p_id:
                    continue

                try:
                    resp = mp_sdk.payment().get(str(p_id))
                    info = resp.get("response") or {}
                    _procesar_pago_desde_info(str(p_id), info)
                except:
                    continue

        return "ok", 200

    except Exception as e:
        app.logger.error(f"[WEBHOOK ERROR] {e}")
        return "ok", 200

@app.post("/webhook")
def mp_webhook_alias1():
    return mp_webhook()

@app.post("/mp/webhook")
def mp_webhook_alias2():
    return mp_webhook()


# =========================
# OAUTH MERCADOPAGO
# =========================

from urllib.parse import urlencode

@app.get("/api/mp/oauth/init")
def mp_oauth_init():
    if not MP_CLIENT_ID or not MP_CLIENT_SECRET:
        return json_error("Faltan CLIENT_ID o CLIENT_SECRET", 500)

    env_redirect = os.getenv("MP_REDIRECT_URI")
    if env_redirect:
        redirect_uri = env_redirect.strip()
    else:
        base = BACKEND_BASE_URL or request.url_root.rstrip("/")
        redirect_uri = f"{base}/api/mp/oauth/callback"

    params = {
        "client_id": MP_CLIENT_ID,
        "response_type": "code",
        "platform_id": "mp",
        "redirect_uri": redirect_uri,
    }
    url = f"https://auth.mercadopago.com/authorization?{urlencode(params)}"
    return ok_json({"url": url})


@app.get("/api/mp/oauth/callback")
def mp_oauth_callback():
    code = request.args.get("code")
    if not code:
        return json_error("Falta code", 400)

    base = BACKEND_BASE_URL or request.url_root.rstrip("/")
    redirect_uri = f"{base}/api/mp/oauth/callback"

    try:
        r = requests.post(
            "https://api.mercadopago.com/oauth/token",
            json={
                "client_id": MP_CLIENT_ID,
                "client_secret": MP_CLIENT_SECRET,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
            },
            timeout=20,
        )
        r.raise_for_status()
    except Exception as e:
        return json_error(f"Error OAuth token: {e}", 500)

    data = r.json() or {}
    access_token = data.get("access_token")
    refresh_token = data.get("refresh_token", "")
    user_id = data.get("user_id")
    expires_in = data.get("expires_in")

    if not access_token:
        return json_error("No se recibió access_token", 500)

    kv_set("mp_oauth_access_token", access_token)
    kv_set("mp_oauth_refresh_token", refresh_token)
    kv_set("mp_oauth_user_id", str(user_id or ""))
    kv_set("mp_oauth_expires", str(expires_in or ""))

    html = """<!doctype html>
<html lang="es">
<head><meta charset="utf-8"/><title>Vinculación correcta</title></head>
<body style="background:#0b1220;color:#e5e7eb;font-family:sans-serif">
<div style="max-width:420px;margin:15vh auto;padding:18px;background:rgba(255,255,255,.05);border-radius:12px">
<h2>Cuenta vinculada</h2>
<p>La cuenta de MercadoPago se vinculó correctamente. Ya podés cerrar esta ventana.</p>
</div>
</body>
</html>"""
    resp = make_response(html, 200)
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    return resp


@app.get("/api/mp/oauth/status")
def mp_oauth_status():
    access_token = kv_get("mp_oauth_access_token", "")
    user_id = kv_get("mp_oauth_user_id", "")
    return ok_json({
        "vinculado": bool(access_token),
        "user_id": user_id,
    })


@app.post("/api/mp/oauth/unlink")
def mp_oauth_unlink():
    kv_set("mp_oauth_access_token", "")
    kv_set("mp_oauth_refresh_token", "")
    kv_set("mp_oauth_user_id", "")
    kv_set("mp_oauth_expires", "")
    return ok_json({"ok": True, "msg": "Desvinculado"})


# =========================
# GRACIAS PAGE
# =========================

@app.get("/gracias")
def pagina_gracias():
    status = (request.args.get("status") or "").lower()

    if status in ("success", "approved"):
        title = "¡Gracias por su compra!"
        msg = "<p>El pago fue aprobado. El dispenser se activará en segundos.</p>"
    elif status in ("pending", "in_process"):
        title = "Pago pendiente"
        msg = "<p>Tu pago está en revisión. Si se aprueba, se activará automáticamente.</p>"
    else:
        title = "Pago no completado"
        msg = "<p>El pago fue cancelado o rechazado.</p>"

    html = f"""<!doctype html>
<html lang="es">
<head><meta charset="utf-8"/></head>
<body style="background:#0b1220;color:#e5e7eb;font-family:Inter,system-ui">
<div style="max-width:720px;margin:16vh auto;padding:20px;background:rgba(255,255,255,.05);border-radius:16px">
<h1>{title}</h1>
{msg}
</div>
</body>
</html>"""

    r = make_response(html, 200)
    r.headers["Content-Type"] = "text/html; charset=utf-8"
    return r


# =========================
# INIT MQTT
# =========================

with app.app_context():
    try:
        start_mqtt_background()
    except Exception:
        app.logger.exception("[MQTT] error iniciando thread")


# =========================
# RUN
# =========================

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
