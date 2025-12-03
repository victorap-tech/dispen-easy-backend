# ============================================================
# Dispen-Agua – Backend MULTI-CLIENTE con OAuth por cliente
# ============================================================

import os
import logging
import threading
import time
import json as _json
from datetime import datetime, timedelta
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
# Auth simple manual (Admin)
# =========================

def require_admin():
    secret = request.headers.get("x-admin-secret", "")
    if not ADMIN_SECRET:
        return
    if secret != ADMIN_SECRET:
        abort(401)

# =========================
# MODELOS
# =========================

class KV(db.Model):
    __tablename__ = "kv"
    key = db.Column(db.String(50), primary_key=True)
    value = db.Column(db.String(200), nullable=False)

# ---------- NUEVO: CLIENTES ----------
class Cliente(db.Model):
    __tablename__ = "cliente"

    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(200), nullable=False)
    descripcion = db.Column(db.String(300), nullable=True)
    created_at = db.Column(db.DateTime, server_default=db.func.now())

    def serialize(self):
      return {
        "id": self.id,
        "nombre": self.nombre,
        "descripcion": self.descripcion,
        "created_at": self.created_at.isoformat() if self.created_at else None
    }

# ---------- TOKENS MP por cliente ----------
class MpTokenPorCliente(db.Model):
    __tablename__ = "mp_token_cliente"

    id = db.Column(db.Integer, primary_key=True)
    cliente_id = db.Column(db.Integer, db.ForeignKey("cliente.id", ondelete="CASCADE"), nullable=False, index=True)

    access_token = db.Column(db.String(500), nullable=False)
    refresh_token = db.Column(db.String(500), nullable=True)
    user_id_mp = db.Column(db.String(200), nullable=True)
    expires_in = db.Column(db.Integer, nullable=True)
    expires_at = db.Column(db.DateTime, nullable=True)

    created_at = db.Column(db.DateTime, server_default=db.func.now())
    actualizado = db.Column(db.DateTime, server_default=db.func.now(), onupdate=db.func.now())

# ---------- DISPENSER con cliente_id ----------
class Dispenser(db.Model):
    __tablename__ = "dispenser"

    id = db.Column(db.Integer, primary_key=True)
    device_id = db.Column(db.String(80), nullable=False, unique=True, index=True)
    nombre = db.Column(db.String(100), nullable=False, default="")
    activo = db.Column(db.Boolean, nullable=False, server_default=db.text("true"))

    # NUEVO → A quién pertenece este dispenser
    cliente_id = db.Column(db.Integer, db.ForeignKey("cliente.id", ondelete="SET NULL"), nullable=True, index=True)

    # estado online/offline por MQTT
    online = db.Column(db.Boolean, nullable=False, server_default=db.text("false"))
    last_seen = db.Column(db.DateTime(timezone=True), nullable=True)

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

    tiempo_ms = db.Column(db.Integer, nullable=False, server_default="2000")

    created_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=db.func.now(), onupdate=db.func.now())

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
        "cliente_id": d.cliente_id,
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
# Auth simple Admin / rutas públicas
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
# MP: modo global (fallback)
# -----------------------

def get_mp_mode() -> str:
    row = KV.query.get("mp_mode")
    return (row.value if row else "test").lower()

def get_global_mp_token_and_base():
    """
    Fallback por si un dispenser no tiene cliente ni OAuth.
    Usa MP_ACCESS_TOKEN_TEST / LIVE.
    """
    mode = get_mp_mode()
    if mode == "live":
        return MP_ACCESS_TOKEN_LIVE, "https://api.mercadopago.com"
    return MP_ACCESS_TOKEN_TEST, "https://api.mercadopago.com"

# -----------------------
# MP: Obtener token por dispenser (MULTI-CLIENTE)
# -----------------------

def get_token_por_dispenser(dispenser_id: int) -> str:
    """
    Devuelve el access_token de MP correspondiente al cliente dueño del dispenser.
    Si el dispenser no tiene cliente o el cliente no tiene OAuth, usa token global.
    """
    disp = Dispenser.query.get(dispenser_id)
    if not disp:
        raise Exception("Dispenser no encontrado")

    # Si tiene cliente asignado, buscamos token OAuth de ese cliente
    if disp.cliente_id:
        tok = MpTokenPorCliente.query.filter_by(cliente_id=disp.cliente_id).first()
        if tok and tok.access_token:
            # Si quisieras, acá podrías refrescar el token si expired.
            return tok.access_token

    # Fallback: tokens globales (modo test/live)
    token, _ = get_global_mp_token_and_base()
    if not token:
        raise Exception("MP token no configurado para este dispenser")
    return token

# =========================
# MQTT
# =========================

_mqtt_client: Optional[mqtt.Client] = None
_mqtt_lock = threading.Lock()

def topic_cmd(device_id: str) -> str:
    return f"dispen/{device_id}/cmd/dispense"

def _mqtt_on_connect(client, userdata, flags, rc, props=None):
    app.logger.info(f"[MQTT] backend conectado al broker rc={rc}")
    client.subscribe("dispen/+/status", qos=1)
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
        disp.last_seen = datetime.utcnow()
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

# =========================
# MQTT THREAD
# =========================

def start_mqtt_background():
    if not MQTT_HOST:
        app.logger.warning("[MQTT] MQTT_HOST no configurado; no se inicia MQTT")
        return

    app.logger.info("[MQTT] Iniciando hilo MQTT...")
    th = threading.Thread(target=_run_mqtt, name="mqtt-thread", daemon=True)
    th.start()

def _run_mqtt():
    global _mqtt_client

    with app.app_context():
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

        try:
            app.logger.info(f"[MQTT] Conectando a {MQTT_HOST}:{MQTT_PORT} ...")
            _mqtt_client.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
        except Exception as e:
            app.logger.error(f"[MQTT] ERROR al conectar: {e}")
            return

        _mqtt_client.loop_forever()

def send_dispense_cmd(device_id: str, payment_id: str, slot_id: int, dispenser_id: int, litros: int = 1) -> bool:
    """
    Envía comando por MQTT al ESP32:
      - payment_id
      - slot_id
      - tiempo_segundos (tomado desde producto.tiempo_ms)
    """
    if not MQTT_HOST:
        app.logger.error("[MQTT] MQTT_HOST no configurado")
        return False

    prod = Producto.query.filter_by(
        dispenser_id=dispenser_id,
        slot_id=slot_id
    ).first()

    if prod:
        tiempo_ms = int(prod.tiempo_ms or 1000)
    else:
        tiempo_ms = 1000

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
                token_global, _ = get_global_mp_token_and_base()
                r2 = requests.get(
                    f"https://api.mercadopago.com/checkout/preferences/{pref_id}",
                    headers={"Authorization": f"Bearer {token_global}"},
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

    pago = Pago.query.filter_by(mp_payment_id=str(payment_id)).first()
    if pago and pago.procesado and status == "approved":
        app.logger.info(f"[WEBHOOK] Pago {payment_id} ya procesado, ignorando duplicado")
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
    })

@app.get("/api/config")
def api_config():
    return ok_json({
        "mp_mode": get_mp_mode(),
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
# CLIENTES (Admin)
# =========================

@app.post("/api/clientes")
def crear_cliente():
    require_admin()
    data = request.get_json(silent=True) or {}
    nombre = (data.get("nombre") or "").strip()

    if not nombre:
        return json_error("nombre requerido", 400)

    descripcion = (data.get("descripcion") or "").strip()

    c = Cliente(nombre=nombre, descripcion=descripcion)
    db.session.add(c)
    db.session.commit()

    return ok_json({"ok": True, "cliente": c.serialize()})

@app.get("/api/clientes")
def listar_clientes():
    return jsonify([c.serialize() for c in Cliente.query.order_by(Cliente.id.asc()).all()])

@app.route("/api/clientes/<int:cid>", methods=["DELETE"])
def delete_cliente(cid):
    require_admin()

    cli = Cliente.query.get(cid)
    if not cli:
        return jsonify({"error": "Cliente no encontrado"}), 404

    # Verificar dispensers asignados
    relacionados = Dispenser.query.filter_by(cliente_id=cid).count()
    if relacionados > 0:
        return jsonify({"error": "El cliente aún tiene dispensers asignados"}), 400

    db.session.delete(cli)
    db.session.commit()

    return jsonify({"msg": "Cliente eliminado correctamente"})

@app.put("/api/cliente/<int:cid>")
def actualizar_cliente(cid):
    require_admin()
    c = Cliente.query.get(cid)
    if not c:
        return json_error("Cliente no encontrado", 404)

    data = request.get_json(silent=True) or {}

    if "nombre" in data:
        c.nombre = (data["nombre"] or "").strip()

    if "descripcion" in data:
       c.descripcion = (data["descripcion"] or "").strip()

    db.session.commit()

    return ok_json({
        "ok": True,
        "cliente": {
            "id": c.id,
            "nombre": c.nombre,
            "descripcion": c.descripcion
        }
    })
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
        cliente_id = data.get("cliente_id")

        if not name:
            all_d = Dispenser.query.order_by(Dispenser.id.asc()).all()
            next_num = len(all_d) + 1
            name = f"dispen-{next_num:02d}"

        device_id = name

        d = Dispenser(
            device_id=device_id,
            nombre=name,
            activo=True,
            cliente_id=cliente_id
        )
        db.session.add(d)
        db.session.commit()

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

@app.post("/api/dispensers/<int:disp_id>/asignar_cliente")
def asignar_cliente(disp_id):
    require_admin()
    data = request.get_json(silent=True) or {}

    # ⚡ PERMITIR liberar el dispenser
    if "cliente_id" not in data:
        return json_error("cliente_id faltante (puede ser null)", 400)

    cliente_id = data.get("cliente_id")

    # ⚡ Si viene null → liberar
    if cliente_id in (None, "", "null"):
        cliente_id = None

    # Si viene un ID, validarlo
    if cliente_id is not None:
        cli = Cliente.query.get(cliente_id)
        if not cli:
            return json_error("cliente_id inválido", 400)

    disp = Dispenser.query.get_or_404(disp_id)
    disp.cliente_id = cliente_id
    db.session.commit()

    return ok_json({
        "ok": True,
        "dispenser_id": disp.id,
        "cliente_id": disp.cliente_id
    })

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

    if Producto.query.filter(
        Producto.dispenser_id == dispenser_id,
        Producto.slot_id == slot
    ).first():
        return json_error("slot ya usado en este dispenser", 409)

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
# PAGOS – PREFERENCIA (Admin) MULTI-CLIENTE
# =========================

@app.post("/api/pagos/preferencia")
def crear_preferencia_api():
    import time as _time
    data = request.get_json(force=True, silent=True) or {}
    product_id = _to_int(data.get("product_id") or 0)

    prod = Producto.query.get(product_id)
    if not prod or not prod.habilitado:
        return json_error("producto no disponible", 400)

    disp = Dispenser.query.get(prod.dispenser_id)
    if not disp or not disp.activo:
        return json_error("dispenser no disponible", 400)

    # TOKEN MULTI-CLIENTE
    try:
        token = get_token_por_dispenser(disp.id)
    except Exception as e:
        return json_error("mp_token_error", 500, str(e))

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
        "binary_mode": False,
        "statement_descriptor": "DISPEN-AGUA"
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
# QR UNIVERSAL MULTI-CLIENTE
# =========================

@app.get("/qr/<device_id>/<int:slot_id>")
def qr_universal(device_id, slot_id):
    """
    Flujo QR 100% multi-cliente.
    Cada dispenser usa el token del cliente dueño.
    """

    # 1) Dispenser
    disp = Dispenser.query.filter_by(device_id=device_id).first()
    if not disp or not disp.activo:
        return "Dispenser no disponible", 404

    # 2) Producto
    prod = Producto.query.filter_by(
        dispenser_id=disp.id,
        slot_id=slot_id
    ).first()

    if not prod or not prod.habilitado:
        return "Producto no disponible", 404

    # 3) TOKEN DEL CLIENTE
    try:
        token = get_token_por_dispenser(disp.id)
    except Exception as e:
        return f"MP token no configurado: {e}", 500

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
        "statement_descriptor": "DISPEN-AGUA"
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

        # Siempre usamos token GLOBAL solo para consultar info.
        token_global, _ = get_global_mp_token_and_base()
        mp_sdk = mercadopago.SDK(token_global)

        # ---- PAYMENT ----
        if "payment" in tipo:
            payment_id = None

            if isinstance(data.get("data"), dict):
                payment_id = data["data"].get("id")

            if not payment_id and data.get("resource"):
                payment_id = str(data["resource"]).split("/")[-1]

            if not payment_id:
                return "ok", 200

            try:
                resp = mp_sdk.payment().get(str(payment_id))
                info = resp.get("response") or {}
                _procesar_pago_desde_info(str(payment_id), info)
            except Exception:
                return "ok", 200

        # ---- MERCHANT ORDER ----
        if "merchant_order" in tipo:
            mo_id = None

            if isinstance(data.get("data"), dict):
                mo_id = data["data"].get("id")

            if not mo_id and data.get("resource"):
                mo_id = str(data["resource"]).split("/")[-1]

            if not mo_id:
                return "ok", 200

            try:
                mo_resp = mp_sdk.merchant_order().get(str(mo_id))
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
# OAUTH MERCADOPAGO (MULTI-CLIENTE)
# =========================

from urllib.parse import urlencode

# ---- INIT ----
@app.get("/api/mp/oauth/init")
def mp_oauth_init():
    """
    Recibe ?cliente_id=XX
    """
    cliente_id = request.args.get("cliente_id", type=int)
    if not cliente_id:
        return json_error("cliente_id requerido", 400)

    if not MP_CLIENT_ID or not MP_CLIENT_SECRET:
        return json_error("Faltan CLIENT_ID o CLIENT_SECRET", 500)

    # redirect URL
    base = BACKEND_BASE_URL or request.url_root.rstrip("/")
    redirect_uri = f"{base}/api/mp/oauth/callback"

    # state = cliente_id  (IMPORTANTE!)
    params = {
        "client_id": MP_CLIENT_ID,
        "response_type": "code",
        "platform_id": "mp",
        "redirect_uri": redirect_uri,
        "state": cliente_id,   # 🔥 Esto permite saber para quién es el token
    }

    url = f"https://auth.mercadopago.com/authorization?{urlencode(params)}"
    return ok_json({"url": url})

# ---- CALLBACK ----
@app.get("/api/mp/oauth/callback")
def mp_oauth_callback():
    code = request.args.get("code")
    cliente_id = request.args.get("state", type=int)

    if not code:
        return json_error("Falta code", 400)
    if not cliente_id:
        return json_error("Falta state=cliente_id", 400)

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

    # Guardar en tabla multi-cliente
    tok = MpTokenPorCliente.query.filter_by(cliente_id=cliente_id).first()
    if not tok:
        tok = MpTokenPorCliente(cliente_id=cliente_id)

    tok.access_token = access_token
    tok.refresh_token = refresh_token
    tok.user_id_mp = str(user_id or "")
    tok.expires_at = expires_in

    db.session.add(tok)
    db.session.commit()

    html = """
    <!doctype html>
    <html lang="es">
    <head><meta charset="utf-8"/><title>Vinculación correcta</title></head>
    <body style="background:#0b1220;color:#e5e7eb;font-family:sans-serif">
    <div style="max-width:420px;margin:15vh auto;padding:18px;background:rgba(255,255,255,.05);border-radius:12px">
    <h2>Cuenta vinculada</h2>
    <p>La cuenta de MercadoPago se vinculó correctamente para este cliente. Ya podés cerrar esta ventana.</p>
    </div>
    </body>
    </html>
    """
    resp = make_response(html, 200)
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    return resp

# ---- STATUS POR CLIENTE ----
@app.get("/api/mp/oauth/status")
def mp_oauth_status():
    cliente_id = request.args.get("cliente_id", type=int)
    if not cliente_id:
        return json_error("cliente_id requerido", 400)

    tok = MpTokenPorCliente.query.filter_by(cliente_id=cliente_id).first()
    if not tok:
        return ok_json({"vinculado": False})

    return ok_json({
        "vinculado": bool(tok.access_token),
        "user_id": tok.user_id_mp,
    })

# ---- UNLINK ----
@app.post("/api/mp/oauth/unlink")
def mp_oauth_unlink():
    cliente_id = request.args.get("cliente_id", type=int)
    if not cliente_id:
        return json_error("cliente_id requerido", 400)

    tok = MpTokenPorCliente.query.filter_by(cliente_id=cliente_id).first()
    if tok:
        db.session.delete(tok)
        db.session.commit()

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

    html = f"""
    <!doctype html>
    <html lang="es">
    <head><meta charset="utf-8"/></head>
    <body style="background:#0b1220;color:#e5e7eb;font-family:Inter,system-ui">
    <div style="max-width:720px;margin:16vh auto;padding:20px;background:rgba(255,255,255,.05);border-radius:16px">
    <h1>{title}</h1>
    {msg}
    </div>
    </body>
    </html>
    """

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

#------------------------------ #      

def watchdog_offline():
    while True:
        try:
            with app.app_context():   # ← ESTA ES LA CLAVE
                limite = datetime.utcnow() - timedelta(seconds=45)
                disps = Dispenser.query.all()

                for d in disps:
                    if d.last_seen and d.last_seen < limite and d.online:
                        d.online = False

                db.session.commit()

        except Exception as e:
            print("Error en watchdog:", e)

        time.sleep(20)


threading.Thread(target=watchdog_offline, daemon=True).start()

# =========================
# INICIAR WATCHDOG (OFFLINE DETECTOR)
# =========================

def start_watchdog():
    try:
        with app.app_context():
        # hilo que marca offline cuando no hay last_seen por 45 segundos
            threading.Thread(target=watchdog_offline, daemon=True).start()
            print("[WATCHDOG] iniciado correctamente")
    except Exception as e:
        print("[WATCHDOG] error iniciando:", e)

start_watchdog()
# =========================
# RUN
# =========================

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
