# app.py – Dispenser Agua (2 productos, sin litros/stock en la UI, con OAuth MP para vincular cuenta del comercio)

import os
import logging
import threading
import time
import json as _json
from typing import Optional

import requests
import paho.mqtt.client as mqtt
import ssl
import mercadopago
import json
from flask import Flask, jsonify, request, make_response, redirect, abort
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
MQTT_PORT = int(os.getenv("MQTT_PORT", 8883))
MQTT_USER = os.getenv("MQTT_USER", "").strip()
MQTT_PASS = os.getenv("MQTT_PASS", "").strip()

mqtt_client = mqtt.Client()

# TLS INSEGURO (igual que ESP32.setInsecure())
mqtt_client.tls_set(
    certfile=None,
    keyfile=None,
    cert_reqs=ssl.CERT_NONE,
    tls_version=ssl.PROTOCOL_TLS,
    ciphers=None
)
mqtt_client.tls_insecure_set(True)

# Usuario / contraseña
mqtt_client.username_pw_set(MQTT_USER, MQTT_PASS)

# Callback conexión
def on_connect(client, userdata, flags, rc):
    if rc == 0:
        app.logger.info("[MQTT] Conectado OK a HiveMQ Cloud")
    else:
        app.logger.error(f"[MQTT] Error al conectar rc={rc}")

mqtt_client.on_connect = on_connect

# Conexión
try:
    mqtt_client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    mqtt_client.loop_start()
except Exception as e:
    app.logger.error(f"[MQTT] Error al iniciar: {e}")

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
# Auth simple manual (para proteger Admin)
# =========================

def require_admin():
    """
    Verifica el header x-admin-secret contra ADMIN_SECRET.
    """
    secret = request.headers.get("x-admin-secret", "")
    if not ADMIN_SECRET:
        return  # admin desactivado
    if secret != ADMIN_SECRET:
        abort(401)

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

    # indica si el dispenser está conectado por MQTT
    online = db.Column(db.Boolean, nullable=False, server_default=db.text("false"))

    created_at = db.Column(
        db.DateTime(timezone=True),
        server_default=db.func.now(),
        nullable=False
    )


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

    # tiempo de dispensado configurado para ese slot
    tiempo_ms = db.Column(db.Integer, nullable=False, server_default="2000")

    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        server_default=db.func.now(),
    )

    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        server_default=db.func.now(),
        onupdate=db.func.now()
    )

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
# Helpers básicos
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
    except Exception:
        try:
            return int(float(x))
        except Exception:
            return default

def serialize_dispenser(d: Dispenser) -> dict:
    return {
        "id": d.id,
        "device_id": d.device_id,
        "nombre": d.nombre,
        "activo": bool(d.activo),
        "online": bool(d.online),
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
        "tiempo_ms": int(p.tiempo_ms),
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
    app.logger.info(f"[MQTT] backend conectado al broker rc={rc}")
    # Pings de estado online/offline
    client.subscribe("dispen/+/status", qos=1)
    # ACK de dispensado
    client.subscribe("dispen/+/state/dispense", qos=1)

def _handle_status_message(topic: str, payload_raw: bytes):
    """
    Maneja mensajes en dispen/<device_id>/status
    Puede ser:
      - "online"
      - JSON {"device": "...", "status": "online|offline|wifi_reconnected|reconnected"}
    """
    try:
        raw = payload_raw.decode().strip()
    except Exception:
        raw = ""

    device_id = topic.split("/")[1] if "/" in topic else ""

    # Caso simple: texto plano
    if raw == "online":
        if device_id:
            disp = Dispenser.query.filter_by(device_id=device_id).first()
            if disp:
                disp.online = True
                db.session.commit()
                app.logger.info(f"[MQTT] {device_id} marcado ONLINE (raw)")
        return

    # Caso JSON
    try:
        data = json.loads(raw) if raw else {}
    except Exception:
        data = {}

    status = str(data.get("status") or "").lower()
    dev = (data.get("device") or device_id).strip()

    if not dev or not status:
        return

    disp = Dispenser.query.filter_by(device_id=dev).first()
    if not disp:
        return

    if status in ("online", "reconnected", "wifi_reconnected"):
        disp.online = True
    elif status == "offline":
        disp.online = False

    try:
        db.session.commit()
        app.logger.info(f"[ONLINE] {dev} → {disp.online}")
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"[ONLINE] Error guardando estado: {e}")

def _handle_ack_message(topic: str, payload_raw: bytes):
    """
    Maneja ACK de dispensado en dispen/<device_id>/state/dispense
    Espera JSON:
      { "pago_id": "...", "slot_id": 1, "dispensado": true }
    """
    try:
        raw = payload_raw.decode().strip()
    except Exception:
        raw = ""

    try:
        data = json.loads(raw) if raw else {}
    except Exception:
        app.logger.error(f"[MQTT] ACK JSON inválido: {raw!r}")
        return

    pago_id = data.get("pago_id") or data.get("payment_id")
    slot_id = data.get("slot_id")
    dispensado = data.get("dispensado")

    if not pago_id or dispensado is not True:
        return

    app.logger.info(f"[MQTT] ACK recibido para pago_id={pago_id}, slot={slot_id}")

    pago = Pago.query.filter_by(mp_payment_id=str(pago_id)).first()
    if not pago:
        app.logger.error(f"[MQTT] Pago {pago_id} no encontrado en DB")
        return

    pago.dispensado = True

    try:
        db.session.commit()
        app.logger.info(f"[MQTT] Pago {pago_id} marcado como DISPENSADO ✔")
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"[MQTT] Error guardando DISPENSADO: {e}")

def _mqtt_on_message(client, userdata, msg):
    app.logger.info(f"[MQTT RX] {msg.topic}: {msg.payload!r}")

    if msg.topic.startswith("dispen/") and msg.topic.endswith("/status"):
        _handle_status_message(msg.topic, msg.payload)
        return

    if msg.topic.startswith("dispen/") and msg.topic.endswith("/state/dispense"):
        _handle_ack_message(msg.topic, msg.payload)
        return

    # Otros topics (si los hubiera) se podrían manejar aquí

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

    # WATCHDOG: si en 40s no llegó ONLINE, marcamos OFFLINE
    def offline_watchdog():
        while True:
            try:
                ds = Dispenser.query.all()
                for d in ds:
                    d.online = False
                db.session.commit()
            except Exception:
                db.session.rollback()
            time.sleep(40)

    threading.Thread(target=offline_watchdog, name="offline-watchdog", daemon=True).start()

def send_dispense_cmd(device_id: str, payment_id: str, slot_id: int, dispenser_id: int, litros: int = 1) -> bool:
    """
    Envía comando por MQTT al ESP32:
      - payment_id
      - slot_id
      - tiempo_segundos (tomado desde producto.tiempo_ms para ese dispenser+slot)
    """

    if not MQTT_HOST:
        app.logger.error("[MQTT] MQTT_HOST no configurado")
        return False

    # Buscar el producto para obtener tiempo configurado
    prod = Producto.query.filter_by(
        dispenser_id=dispenser_id,
        slot_id=slot_id
    ).first()

    if prod:
        tiempo_ms = int(prod.tiempo_ms or 1000)
    else:
        tiempo_ms = 1000  # fallback seguro

    tiempo_segundos = max(1, int(tiempo_ms / 1000))

    topic = f"dispen/{device_id}/cmd/dispense"

    payload = json.dumps({
        "payment_id": str(payment_id),
        "slot_id": int(slot_id),
        "tiempo_segundos": tiempo_segundos
    })

    app.logger.info(
        f"[MQTT] → {topic} | tiempo_ms={tiempo_ms}, tiempo_segundos={tiempo_segundos}, payload={payload}"
    )

    # Publicación MQTT robusta
    for intento in range(10):
        with _mqtt_lock:
            if _mqtt_client:
                info = _mqtt_client.publish(topic, payload, qos=1, retain=False)
                if info.rc == mqtt.MQTT_ERR_SUCCESS:
                    return True
        time.sleep(0.3)

    app.logger.error("[MQTT] ERROR: no se pudo publicar el comando después de 10 intentos")
    return False

# =========================
# PROCESAR PAGO
# =========================

def _procesar_pago_desde_info(payment_id: str, info: dict):
    status_raw = info.get("status")
    status = str(status_raw).lower() if status_raw is not None else ""
    metadata = info.get("metadata") or {}

    app.logger.info(f"[WEBHOOK] payment_id={payment_id} status={status} metadata={metadata}")

    # Recuperar metadata si MP no la envía
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
                pref_info = r2.json() or {}
                meta2 = pref_info.get("metadata") or {}
                if meta2:
                    metadata = meta2
                    app.logger.info("[WEBHOOK] Metadata recuperada desde preferencia")
        except Exception as e:
            app.logger.error(f"[WEBHOOK] No se pudo recuperar metadata: {e}")

    product_id   = _to_int(metadata.get("product_id") or 0)
    slot_id      = _to_int(metadata.get("slot_id") or 0)
    dispenser_id = _to_int(metadata.get("dispenser_id") or 0)
    device_id    = (metadata.get("device_id") or "").strip()
    litros_md    = _to_int(metadata.get("litros") or 1)
    producto_nom = metadata.get("producto") or info.get("description") or ""
    monto_val    = info.get("transaction_amount") or metadata.get("precio_final") or 0

    if not device_id or not slot_id:
        app.logger.error(f"[WEBHOOK] Falta device_id o slot_id en payment_id={payment_id}")
        return "ok", 200

    # 🔐 ANTI-DUPLICADOS: si ya está procesado y viene otra vez approved, no hago nada
    pago = Pago.query.filter_by(mp_payment_id=str(payment_id)).first()
    if pago and pago.procesado and status == "approved":
        app.logger.info(f"[WEBHOOK] Pago {payment_id} ya procesado, ignorando duplicado")
        return

    # Crear o actualizar registro
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

    # Solo cuando queda en approved y NO estaba procesado todavía
    if status == "approved" and not pago.procesado:
        ok = send_dispense_cmd(device_id, payment_id, slot_id, dispenser_id, litros_md)
        if ok:
            pago.procesado = True
            db.session.commit()
            app.logger.info(f"[WEBHOOK] Pago {payment_id} marcado como procesado")
        else:
            app.logger.error(f"[MQTT] ERROR al enviar comando a {device_id}")

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

        # Creamos 2 productos base para ese dispenser
        p1 = Producto(
            dispenser_id=d.id,
            nombre="Agua fría",
            precio=0,
            cantidad=0,
            slot_id=1,
            habilitado=False,
        )
        p2 = Producto(
            dispenser_id=d.id,
            nombre="Agua caliente",
            precio=0,
            cantidad=0,
            slot_id=2,
            habilitado=False,
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

    q = q.order_by(Producto.slot_id.asc()).all()

    return jsonify([serialize_producto(p) for p in q])

@app.post("/api/productos")
def api_productos_create():
    require_admin()

    data = request.get_json(silent=True) or {}
    dispenser_id = data.get("dispenser_id")
    nombre = (data.get("nombre") or "").strip()
    precio = data.get("precio")
    slot = data.get("slot")
    habilitado = bool(data.get("habilitado", True))
    tiempo_ms = data.get("tiempo_ms")

    if not dispenser_id:
        return json_error("dispenser_id requerido", 400)

    if not nombre:
        return json_error("nombre requerido", 400)

    try:
        precio = float(precio)
    except Exception:
        return json_error("precio debe ser número", 400)

    if precio <= 0:
        return json_error("precio debe ser > 0", 400)

    try:
        slot = int(slot)
    except Exception:
        return json_error("slot debe ser número", 400)

    if not 1 <= slot <= 2:
        return json_error("slot inválido (1–2)", 400)

    # Revisar que no exista otro producto en ese slot
    if Producto.query.filter(
        Producto.dispenser_id == dispenser_id,
        Producto.slot_id == slot
    ).first():
        return json_error("slot ya usado en este dispenser", 409)

    # Normalizar tiempo_ms
    try:
        if tiempo_ms not in (None, "", []):
            tiempo_final = int(tiempo_ms)
        else:
            tiempo_final = 1000
    except Exception:
        tiempo_final = 1000

    p = Producto(
        dispenser_id=dispenser_id,
        nombre=nombre,
        precio=precio,
        cantidad=0,
        slot_id=slot,
        porcion_litros=1,
        bundle_precios={},
        habilitado=habilitado,
        tiempo_ms=tiempo_final,
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

        # Actualizar el tiempo de dispensado
        if "tiempo_ms" in data:
            try:
                if data["tiempo_ms"] not in ("", None):
                    p.tiempo_ms = int(data["tiempo_ms"])
            except Exception:
                pass

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
    if not device and p.product_id:
        prod = Producto.query.get(p.product_id)
        if prod and prod.dispenser_id:
            d = Dispenser.query.get(prod.dispenser_id)
            device = d.device_id if d else ""

    if not device:
        return json_error("sin device_id", 400)

    ok = send_dispense_cmd(device, p.mp_payment_id, p.slot_id, p.dispenser_id, p.litros or 1)
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
    import time as _time
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
    ts = int(_time.time())

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
    Cada vez que alguien escanea este QR:
      1) Buscamos el dispenser por device_id
      2) Buscamos el producto por dispenser_id + slot_id
      3) Creamos una NUEVA preferencia de MercadoPago
      4) Redirigimos (302) al init_point de esa preferencia
    """

    # 1) Buscar dispenser por device_id
    disp = Dispenser.query.filter_by(device_id=device_id).first()
    if not disp or not disp.activo:
        return "Dispenser no disponible", 404

    # 2) Buscar producto por slot_id
    prod = Producto.query.filter_by(
        dispenser_id=disp.id,
        slot_id=slot_id
    ).first()

    if not prod or not prod.habilitado:
        return "Producto no disponible", 404

    # 3) Obtener token MP
    token, _base_api = get_mp_token_and_base()
    if not token:
        return "MP token no configurado", 500

    backend_url = get_backend_base()
    monto_final = float(prod.precio)

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

    # 4) Crear preferencia
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

        # Detecta cualquier formato de MP
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
            except Exception:
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
            except Exception:
                return "ok", 200

            for pay in mo_info.get("payments") or []:
                p_id = pay.get("id")
                if not p_id:
                    continue

                try:
                    resp = mp_sdk.payment().get(str(p_id))
                    info = resp.get("response") or {}
                    _procesar_pago_desde_info(str(p_id), info)
                except Exception:
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
