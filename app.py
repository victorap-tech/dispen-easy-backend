# app.py
import os
import logging
import threading
import requests
import json as _json
import json 
import time
import secrets
from collections import defaultdict
from queue import Queue
from threading import Lock

from flask import Flask, jsonify, request, make_response, Response, render_template, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy import UniqueConstraint, text as sqltext, and_
from datetime import datetime
import paho.mqtt.client as mqtt

# --- FUNCIÓN PARA ENVIAR MENSAJES POR TELEGRAM ---
def send_telegram_message(chat_id, text):
    """
    Envía un mensaje simple por Telegram al chat_id indicado.
    Usa el BOT_TOKEN definido en las variables de entorno.
    """
    import os, requests
    TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
    if not TOKEN or not chat_id:
        print("⚠️ No hay BOT_TOKEN o chat_id definido, no se envía mensaje.")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10
        )
        print(f"📨 Mensaje enviado a {chat_id}: {text}")
    except Exception as e:
        print("❌ Error al enviar mensaje de Telegram:", e)

# ============ Helpers ============

def ok_json(data, status=200): return jsonify(data), status
def json_error(msg, status=400, extra=None):
    p = {"error": msg}
    if extra is not None: p["detail"] = extra
    return jsonify(p), status

def _to_int(x, default=0):
    try: return int(x)
    except Exception:
        try: return int(float(x))
        except Exception: return default

# ============ Config ============

DATABASE_URL = os.getenv("DATABASE_URL", "")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

BACKEND_BASE_URL = (os.getenv("BACKEND_BASE_URL", "") or "").rstrip("/")
WEB_URL = os.getenv("WEB_URL", "https://example.com").strip().rstrip("/")

MP_ACCESS_TOKEN_TEST = os.getenv("MP_ACCESS_TOKEN_TEST", "").strip()
MP_ACCESS_TOKEN_LIVE = os.getenv("MP_ACCESS_TOKEN_LIVE", "").strip()

MQTT_HOST = os.getenv("MQTT_HOST", "").strip()
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883") or 1883)
MQTT_USER = os.getenv("MQTT_USER", "")
MQTT_PASS = os.getenv("MQTT_PASS", "")

# acepta cualquiera de los dos nombres
def _admin_env():
    raw = (os.getenv("ADMIN_SECRET") or os.getenv("ADMIN_TOKEN") or "").strip().strip("'").strip('"')
    return raw

# ============ App/DB/CORS ============

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL or "sqlite:///local.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False


CORS(
    app,
    resources={r"/api/*": {"origins": ["https://dispen-easy-web-production.up.railway.app"]}},
    allow_headers=["Content-Type", "x-admin-secret", "x-admin-token", "x-operator-token"],
    expose_headers=["Content-Type"],
)

db = SQLAlchemy(app)
logging.basicConfig(level=logging.INFO)
app.logger.setLevel(logging.INFO)

# ============ Modelos ============

class KV(db.Model):
    __tablename__ = "kv"
    key = db.Column(db.String(50), primary_key=True)
    value = db.Column(db.String(200), nullable=False)

class Dispenser(db.Model):
    __tablename__ = "dispenser"
    id = db.Column(db.Integer, primary_key=True)
    device_id = db.Column(db.String(80), nullable=False, unique=True, index=True)
    nombre = db.Column(db.String(100), nullable=True, default="")
    activo = db.Column(db.Boolean, nullable=False, server_default=db.text("true"))
    created_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now(), nullable=False)

class Producto(db.Model):
    __tablename__ = "producto"
    id = db.Column(db.Integer, primary_key=True)
    dispenser_id = db.Column(db.Integer, db.ForeignKey("dispenser.id", ondelete="SET NULL"), nullable=True, index=True)
    nombre = db.Column(db.String(100), nullable=False)
    precio = db.Column(db.Float, nullable=False)          # $ por litro
    cantidad = db.Column(db.Integer, nullable=False)      # stock (L)
    slot_id = db.Column(db.Integer, nullable=False)       # 1..6
    porcion_litros = db.Column(db.Integer, nullable=False, server_default="1")
    bundle_precios = db.Column(JSONB, nullable=True)      # {"2": 1800, "3": 2600}
    habilitado = db.Column(db.Boolean, nullable=False, server_default=db.text("false"))
    created_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now(), nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), onupdate=db.func.now())
    __table_args__ = (UniqueConstraint("dispenser_id", "slot_id", name="uq_disp_slot"),)

    def to_dict(self):
        """Convierte el producto a diccionario JSON-friendly"""
        return {
            "id": self.id,
            "dispenser_id": self.dispenser_id,
            "nombre": self.nombre,
            "precio": self.precio,
            "cantidad": self.cantidad,
            "porcion_litros": self.porcion_litros,
            "habilitado": self.habilitado,
            "bundle_precios": self.bundle_precios or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None
        }

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

class OperatorToken(db.Model):
    __tablename__ = "operator_token"
    token = db.Column(db.String(80), primary_key=True, unique=True, index=True, default=lambda: secrets.token_urlsafe(24))
    dispenser_id = db.Column(db.Integer, db.ForeignKey("dispenser.id", ondelete="CASCADE"), nullable=False, index=True)
    nombre = db.Column(db.String(100), nullable=True, default="")
    activo = db.Column(db.Boolean, nullable=False, server_default=db.text("true"))
    created_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now(), nullable=False)
    chat_id = db.Column(db.String(40), nullable=True, default="")  # Telegram del cliente
    mp_access_token = db.Column(db.String(255), nullable=True)  # Token propio de MercadoPago (OAuth)

with app.app_context():
    db.create_all()
    if not KV.query.get("mp_mode"):
        db.session.add(KV(key="mp_mode", value="test")); db.session.commit()
    if Dispenser.query.count() == 0:
        db.session.add(Dispenser(device_id="dispen-01", nombre="dispen-01 (por defecto)", activo=True))
        db.session.commit()
    try:
        db.session.execute(sqltext("ALTER TABLE producto ADD COLUMN IF NOT EXISTS bundle_precios JSONB"))
        db.session.commit()
    except Exception:
        db.session.rollback()
    try:
        db.session.execute(sqltext("CREATE INDEX IF NOT EXISTS operator_token_dispenser_id_idx ON operator_token(dispenser_id)"))
        db.session.commit()
    except Exception:
        db.session.rollback()
    try:
        db.session.execute(sqltext("ALTER TABLE operator_token ADD COLUMN IF NOT EXISTS mp_access_token VARCHAR(255)"))
        db.session.commit()
    except Exception:
        db.session.rollback()

# ============ Serializers y utils ============

def serialize_producto(p: Producto) -> dict:
    return {
        "id": p.id, "dispenser_id": p.dispenser_id, "nombre": p.nombre,
        "precio": float(p.precio), "cantidad": int(p.cantidad), "slot": int(p.slot_id),
        "porcion_litros": int(getattr(p, "porcion_litros", 1) or 1),
        "bundle_precios": p.bundle_precios or {},
        "habilitado": bool(p.habilitado),
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }

def serialize_dispenser(d: Dispenser) -> dict:
    # Buscar el operador activo asociado
    op = OperatorToken.query.filter_by(dispenser_id=d.id, activo=True).first()
    operator_name = op.nombre if op else None

    return {
        "id": d.id,
        "device_id": d.device_id,
        "nombre": d.nombre or "",
        "estado": "Activo" if d.activo else "Suspendido",
        "ubicacion": getattr(d, "ubicacion", None),
        "operator": operator_name,
        "activo": bool(d.activo),
        "created_at": d.created_at.isoformat() if getattr(d, "created_at", None) else None,
    }
    
def get_thresholds():
    reserva = max(0, int(os.getenv("STOCK_RESERVA_LTS", "1") or 1))
    umbral_cfg = max(0, int(os.getenv("UMBRAL_ALERTA_LTS", "3") or 3))
    umbral = umbral_cfg if umbral_cfg > reserva else (reserva + 1)
    return umbral, reserva

# ============ Notificaciones Telegram ============

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "").strip()

def _tg_send(text: str, chat_id: str = None):
    token = TELEGRAM_BOT_TOKEN
    if not token: 
        app.logger.warning("[TG] TOKEN no configurado; mensaje no enviado")
        return
    dest = chat_id or TELEGRAM_CHAT_ID
    if not dest: 
        app.logger.warning("[TG] CHAT_ID no configurado; mensaje no enviado")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": dest, "text": text},
            timeout=10
        )
    except Exception as e:
        app.logger.error(f"[TG] Error enviando notificación: {e}")

def tg_notify_admin(text: str): _tg_send(text, TELEGRAM_CHAT_ID)

def tg_notify_all(text: str, dispenser_id: int | None = None):
    tg_notify_admin(text)
    if dispenser_id:
        toks = OperatorToken.query.filter(and_(OperatorToken.dispenser_id == dispenser_id, OperatorToken.activo == True)).all()
        for t in toks:
            if (t.chat_id or "").strip():
                _tg_send(text, t.chat_id.strip())

def _post_stock_change_hook(prod: "Producto", motivo: str, operator_name: str = None):
    umbral, reserva = get_thresholds()
    stock = int(prod.cantidad or 0)
    note = f" – {motivo}"
    if operator_name:
        note += f" (operator: {operator_name})"
    if stock <= umbral:
        tg_notify_all(
            f"⚠️ Bajo stock '{prod.nombre}' (disp {prod.dispenser_id}, slot {prod.slot_id}): "
            f"{stock} L (umbral={umbral}, reserva={reserva}){note}",
            dispenser_id=prod.dispenser_id
        )
    if stock < reserva:
        if prod.habilitado:
            prod.habilitado = False
            app.logger.info(f"[STOCK] Deshabilitado '{prod.nombre}' disp={prod.dispenser_id} (stock={stock})")
    else:
        if not prod.habilitado:
            prod.habilitado = True
            app.logger.info(f"[STOCK] Re-habilitado '{prod.nombre}' disp={prod.dispenser_id} (stock={stock})")

# ============ Admin guard ============

def _admin_header():
    return (request.headers.get("x-admin-secret")
            or request.headers.get("x_admin_secret")
            or request.headers.get("x-admin-token")
            or request.headers.get("x_admin_token")
            or request.environ.get("HTTP_X_ADMIN_SECRET")
            or request.environ.get("HTTP_X_ADMIN_TOKEN")
            or "").strip().strip("'").strip('"')

@app.before_request
def _auth_guard():
    if request.method == "OPTIONS":
        return "", 200

    p = request.path
    PUBLIC_PATHS = {
        "/", "/gracias", "/sin-stock",
        "/vincular_mp", "/operator_login",
        "/operator",
        "/api/mp/webhook", "/webhook", "/mp/webhook",
        "/api/mp/oauth_start", "/api/mp/oauth_callback",
        "/api/pagos/preferencia", "/api/pagos/pendiente",
        "/api/config", "/go", "/ui/seleccionar",
        "/api/productos/opciones",
        "/api/operator/productos", "/api/operator/productos/reponer",
        "/api/operator/productos/reset", "/api/operator/link",
        "/api/_debug/admin", "/api/debug/last_status",
        "/api/dispensers/status",
    }
    if p in PUBLIC_PATHS or (p.startswith("/api/productos/") and p.endswith("/opciones")) or p.startswith("/api/operator/"):
        return None

    env = _admin_env()
    if not env:
        return None  # sin password -> libre (dev)

    hdr = _admin_header()
    if hdr != env:
        return json_error("unauthorized", 401)
    return None

# ============ MP tokens ============

def get_mp_mode() -> str:
    row = KV.query.get("mp_mode")
    return (row.value if row else "test").lower()

def get_mp_token_and_base() -> tuple[str, str]:
    mode = get_mp_mode()
    if mode == "live":
        return MP_ACCESS_TOKEN_LIVE, "https://api.mercadopago.com"
    return MP_ACCESS_TOKEN_TEST, "https://api.sandbox.mercadopago.com"

# ============ MQTT + SSE ============

_mqtt_client = None
_mqtt_lock = threading.Lock()
def topic_cmd(device_id: str) -> str: return f"dispen/{device_id}/cmd/dispense"
def topic_state_wild() -> str: return "dispen/+/state/dispense"
def topic_status_wild() -> str: return "dispen/+/status"
def topic_event_wild() -> str: return "dispen/+/event"

# SSE infra
_sse_clients = []; _sse_lock = Lock()
def _sse_broadcast(data: dict):
    with _sse_lock:
        dead = []
        for q in _sse_clients:
            try: q.put_nowait(data)
            except Exception: dead.append(q)
        for q in dead:
            try: _sse_clients.remove(q)
            except Exception: pass

@app.get("/api/events/stream")
def sse_stream():
    import os

    # 🧩 Tomamos el secreto desde variable de entorno o valor por defecto
    env_secret = os.getenv("ADMIN_SECRET", "adm123")

    # 🧩 Aceptamos tanto ?secret como headers
    secret = (
        request.args.get("secret")
        or request.headers.get("x-admin-secret")
        or request.headers.get("x-admin-token")
        or ""
    )

    # 🧠 Verificamos que el secreto sea correcto
    if secret.strip() != env_secret.strip():
        print(f"⚠️ SSE rechazado. Recibido: {secret} | Esperado: {env_secret}")
        return json_error("unauthorized", 401)

    # 🧱 Creamos la cola para eventos
    q = Queue(maxsize=100)
    with _sse_lock:
        _sse_clients.append(q)

    # 🔁 Función generadora que envía los datos
    def gen():
        yield "retry: 5000\n\n"
        try:
            while True:
                data = q.get()
                yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
        except GeneratorExit:
            with _sse_lock:
                try:
                    _sse_clients.remove(q)
                except Exception:
                    pass

    return Response(gen(), mimetype="text/event-stream")

# ===================== Control de estados ONLINE / OFFLINE =====================
from collections import defaultdict
import threading, time

_last_notified_status = defaultdict(lambda: "")
_online_timers = {}
last_status = {}

ON_DEBOUNCE_S = 5   # segundos para confirmar ONLINE
OFF_DEBOUNCE_S = 0  # sin demora para OFFLINE

def _device_notify(dev: str, status: str):
    """Envía notificación a Telegram solo si hay cambio de estado."""
    disp = Dispenser.query.filter(Dispenser.device_id == dev).first()
    disp_id = disp.id if disp else None
    icon = "✅" if status == "online" else "⚠️"

    last_state = _last_notified_status[dev]
    if last_state != status:
        _last_notified_status[dev] = status
        tg_notify_all(f"{icon} {dev}: {status.upper()}", dispenser_id=disp_id)
        app.logger.info(f"[NOTIFY] Cambio detectado → {dev}: {status}")
    else:
        app.logger.info(f"[NOTIFY] Ignorado {dev} {status} (sin cambio)")

def _schedule_online_notify(dev: str, ts_mark: float):
    """Espera unos segundos antes de confirmar ONLINE."""
    def _do():
        rec = last_status.get(dev, {"status": "unknown", "t": 0})
        if rec["status"] != "online" or rec["t"] < ts_mark:
            return
        if _last_notified_status[dev] != "online":
            _last_notified_status[dev] = "online"
            with app.app_context():
                _device_notify(dev, "online")

    t_old = _online_timers.get(dev)
    try:
        if t_old:
            t_old.cancel()
    except Exception:
        pass

    t = threading.Timer(ON_DEBOUNCE_S, _do)
    t.daemon = True
    _online_timers[dev] = t
    t.start()

def _mqtt_on_connect(client, userdata, flags, rc, props=None):
    app.logger.info(f"[MQTT] conectado rc={rc}; subscribe {topic_state_wild()} {topic_status_wild()} {topic_event_wild()}")
    client.subscribe(topic_state_wild(), qos=1)
    client.subscribe(topic_status_wild(), qos=1)
    client.subscribe(topic_event_wild(), qos=1)

def _mqtt_on_message(client, userdata, msg):
    try: raw = msg.payload.decode("utf-8", "ignore")
    except Exception: raw = "<binario>"
    app.logger.info(f"[MQTT] RX topic={msg.topic} payload={raw}")

    # Pulsador físico → SSE
    if msg.topic.startswith("dispen/") and msg.topic.endswith("/event"):
        try: data = _json.loads(raw or "{}")
        except Exception: data = {}
        if str(data.get("event") or "") == "button_press":
            try: dev = msg.topic.split("/")[1]
            except Exception: dev = ""
            slot = int(data.get("slot") or 0)
            _sse_broadcast({"type":"button_press","device_id":dev,"slot":slot})
        return

   # Estado ONLINE/OFFLINE
    if msg.topic.startswith("dispen/") and msg.topic.endswith("/status"):
        try:
            data = _json.loads(raw or "{}")
        except Exception:
            return

        dev = str(data.get("device") or "").strip()
        st = str(data.get("status") or "").lower().strip()
        if not dev or st not in ("online", "offline"):
            return

        now = time.time()
        last_status[dev] = {"status": st, "t": now}
        _sse_broadcast({"type": "device_status", "device_id": dev, "status": st})

        if st == "offline":
            # cancelar timer de ONLINE
            t_old = _online_timers.get(dev)
            try:
                if t_old:
                    t_old.cancel()
            except Exception:
                pass
            with app.app_context():
                _device_notify(dev, "offline")
            return

        if st == "online":
            # 🔥 Notificar inmediatamente, sin debounce
            with app.app_context():
                _device_notify(dev, "online")
            app.logger.info(f"[MQTT] Notificación ONLINE inmediata para {dev}")
            return
    # Estado dispensa → actualizar stock si llega "done"
    try: data = _json.loads(raw or "{}")
    except Exception: return
    payment_id = str(data.get("payment_id") or "").strip()
    status = str(data.get("status") or "").lower()
    if status in ("ok", "finish", "finished", "success"): status = "done"
    if not payment_id or status not in ("done", "error", "timeout"): return
    with app.app_context():
        p = Pago.query.filter_by(mp_payment_id=payment_id).first()
        if not p: return
        if status == "done" and not p.dispensado:
            try:
                litros_desc = int(p.litros or 0) or 1
                prod = Producto.query.get(p.product_id) if p.product_id else None
                if prod:
                    prod.cantidad = max(0, int(prod.cantidad or 0) - litros_desc)
                    _post_stock_change_hook(prod, motivo="dispensado (ESP done)")
                p.dispensado = True
                db.session.commit()
            except Exception:
                db.session.rollback()

def start_mqtt_background():
    if not MQTT_HOST:
        app.logger.warning("[MQTT] MQTT_HOST no configurado; no se inicia MQTT")
        return
    def _run():
        global _mqtt_client
        _mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="dispen-backend")
        if MQTT_USER or MQTT_PASS: _mqtt_client.username_pw_set(MQTT_USER, MQTT_PASS)
        if int(MQTT_PORT) == 8883:
            try: _mqtt_client.tls_set()
            except Exception as e: app.logger.error(f"[MQTT] TLS: {e}")
        _mqtt_client.on_connect = _mqtt_on_connect
        _mqtt_client.on_message = _mqtt_on_message
        _mqtt_client.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
        _mqtt_client.loop_forever()
    threading.Thread(target=_run, name="mqtt-thread", daemon=True).start()

# ============ Health/Config ============

@app.get("/")
def health(): return ok_json({"status": "ok"})

@app.get("/api/config")
def api_get_config():
    umbral, reserva = get_thresholds()
    return ok_json({"mp_mode": get_mp_mode(), "umbral_alerta_lts": umbral, "stock_reserva_lts": reserva})

@app.post("/api/mp/mode")
def api_set_mode():
    data = request.get_json(force=True, silent=True) or {}
    mode = str(data.get("mode") or "").lower()
    if mode not in ("test", "live"): return json_error("modo inválido (test|live)", 400)
    kv = KV.query.get("mp_mode") or KV(key="mp_mode", value=mode); kv.value = mode
    db.session.merge(kv); db.session.commit()
    return ok_json({"ok": True, "mp_mode": mode})
# =====================================
# VINCULACIÓN MERCADOPAGO POR OPERADOR (OAuth)
# =====================================

@app.get("/api/mp/oauth_start")
def mp_oauth_start():
    app.logger.info(f"[DEBUG] Headers recibidos en /api/mp/oauth_start: {dict(request.headers)}")
    app.logger.info(f"[DEBUG] Args recibidos: {request.args}")
    """Devuelve la URL de autorización de MercadoPago para vincular una cuenta."""
    operator = _operator_from_header()
    if not operator:
        return json_error("Token de operador inválido", 401)

    client_id = os.getenv("MP_CLIENT_ID", "")
    if not client_id:
        return json_error("CLIENT_ID no configurado", 500)

    # ✅ Usa BACKEND_BASE_URL si existe, o el dominio actual como respaldo
    redirect_base = BACKEND_BASE_URL or request.url_root.rstrip("/")
    redirect_uri = f"{redirect_base}/api/mp/oauth_callback"

    url = (
        f"https://auth.mercadopago.com.ar/authorization?"
        f"response_type=code&client_id={client_id}"
        f"&redirect_uri={redirect_uri}&state={operator.token}"
    )
    return ok_json({"ok": True, "url": url})


@app.get("/api/mp/oauth_callback")
def mp_oauth_callback():
    """Callback que MercadoPago llama luego de la autorización."""
    code = request.args.get("code")
    state = request.args.get("state")  # token del operador
    if not code or not state:
        return json_error("Faltan parámetros", 400)

    op = OperatorToken.query.filter_by(token=state).first()
    if not op:
        return json_error("Token de operador inválido", 401)

    client_id = os.getenv("MP_CLIENT_ID", "")
    client_secret = os.getenv("MP_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        return json_error("Credenciales MP faltantes", 500)

    # ✅ Igual que arriba, usamos redirect_base para que funcione en Railway o local
    redirect_base = BACKEND_BASE_URL or request.url_root.rstrip("/")
    redirect_uri = f"{redirect_base}/api/mp/oauth_callback"

    # Pedimos el access_token a MercadoPago
    import requests
    token_url = "https://api.mercadopago.com/oauth/token"
    data = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "redirect_uri": redirect_uri,
    }

    r = requests.post(token_url, data=data)
    if r.status_code != 200:
        return json_error(f"Error al obtener token: {r.text}", 400)

    mp_data = r.json()
    access_token = mp_data.get("access_token")
    user_id = mp_data.get("user_id")

    if not access_token:
        return json_error("MercadoPago no devolvió access_token", 400)

    op.mp_access_token = access_token
    op.mp_user_id = str(user_id)
    db.session.commit()

    return ok_json({"ok": True, "mensaje": "Cuenta vinculada correctamente"})

# ============ Dispensers/Productos CRUD ============

@app.get("/api/dispensers")
def dispensers_list():
    ds = Dispenser.query.order_by(Dispenser.id.asc()).all()
    data = []
    for d in ds:
        operator_name = None
        if getattr(d, "operator_token_id", None):
            op = OperatorToken.query.filter_by(id=d.operator_token_id).first()
            if op:
                operator_name = getattr(op, "nombre", None) or getattr(op, "usuario", None) or "Operador asignado"

        data.append({
            "id": d.id,
            "nombre": getattr(d, "nombre", None),
            "device_id": getattr(d, "device_id", None),
            "ubicacion": getattr(d, "ubicacion", None),
            "estado": "Activo" if d.activo else "Suspendido",  # ✅ CORRECTO
            "activo": bool(d.activo),                         # ✅ NUEVO CAMPO EXPLÍCITO
            "operator": operator_name,
        })
    return jsonify(data)
    
def require_admin():
    env = _admin_env()
    hdr = _admin_header()
    print(f"[ADMIN DEBUG] Header={repr(hdr)} Env={repr(env)}")
    if env and hdr != env:
        return jsonify({"error": "unauthorized"}), 401
    return None

@app.put("/api/dispensers/<int:did>")
def dispensers_update(did):
    ra = require_admin()
    if ra:
        return ra

    d = Dispenser.query.get_or_404(did)
    data = request.get_json(force=True, silent=True) or {}

    try:
        # --- Actualiza solo los campos presentes en el JSON recibido ---
        if "activo" in data:
            d.activo = bool(data["activo"])
            d.estado = "Activo" if d.activo else "Suspendido"
            app.logger.info(f"🟢 Dispenser {d.id} {'activado' if d.activo else 'suspendido'}")

        if "nombre" in data:
            d.nombre = data["nombre"]

        if "ubicacion" in data:
            d.ubicacion = data["ubicacion"]

        if "operator" in data:
            d.operator = data["operator"]

        # Mantener valores previos si no se enviaron
        if "operator" not in data and hasattr(d, "operator"):
            d.operator = d.operator
        if "nombre" not in data and hasattr(d, "nombre"):
            d.nombre = d.nombre

        db.session.commit()
        app.logger.info(f"✅ Dispenser {d.id} actualizado correctamente.")
        return jsonify(serialize_dispenser(d))

    except Exception as e:
        db.session.rollback()
        app.logger.error(f"❌ Error al actualizar dispenser {d.id}: {e}")
        return jsonify({"error": str(e)}), 500
        
@app.get("/api/productos")
def productos_list():
    disp_id = _to_int(request.args.get("dispenser_id") or 0)
    q = Producto.query
    if disp_id: q = q.filter(Producto.dispenser_id == disp_id)
    prods = q.order_by(Producto.dispenser_id.asc(), Producto.slot_id.asc()).all()
    return jsonify([serialize_producto(p) for p in prods])

@app.post("/api/productos")
def productos_create():
    data = request.get_json(force=True, silent=True) or {}
    try:
        disp_id = _to_int(data.get("dispenser_id") or 0)
        if not disp_id or not Dispenser.query.get(disp_id):
            return json_error("dispenser_id inválido", 400)
        p = Producto(
            dispenser_id=disp_id,
            nombre=str(data.get("nombre", "")).strip(),
            precio=float(data.get("precio", 0)),
            cantidad=int(float(data.get("cantidad", 0))),
            slot_id=int(data.get("slot", 1)),
            porcion_litros=int(data.get("porcion_litros", 1)),
            habilitado=bool(data.get("habilitado", False)),
            bundle_precios=data.get("bundle_precios") or {},
        )
        if p.precio < 0 or p.cantidad < 0 or p.porcion_litros < 1:
            return json_error("Valores inválidos", 400)
        if Producto.query.filter(Producto.dispenser_id == p.dispenser_id, Producto.slot_id == p.slot_id).first():
            return json_error("Slot ya asignado a otro producto en este dispenser", 409)
        db.session.add(p); _post_stock_change_hook(p, motivo="create"); db.session.commit()
        return ok_json({"ok": True, "producto": serialize_producto(p)}, 201)
    except Exception as e:
        db.session.rollback()
        app.logger.exception("Error creando producto")
        return json_error("Error creando producto", 500, str(e))

@app.put("/api/productos/<int:pid>")
def productos_update(pid):
    data = request.get_json(force=True, silent=True) or {}
    p = Producto.query.get_or_404(pid)
    try:
        before = int(p.cantidad or 0)
        if "dispenser_id" in data:
            new_d = _to_int(data["dispenser_id"])
            if new_d and new_d != p.dispenser_id and Dispenser.query.get(new_d):
                if Producto.query.filter(Producto.dispenser_id == new_d, Producto.slot_id == p.slot_id).first():
                    return json_error("Slot ya usado en el nuevo dispenser", 409)
                p.dispenser_id = new_d
        if "nombre" in data: p.nombre = str(data["nombre"]).strip()
        if "precio" in data: p.precio = float(data["precio"])
        if "cantidad" in data: p.cantidad = int(float(data["cantidad"]))
        if "porcion_litros" in data:
            val = int(data["porcion_litros"])
            if val < 1: return json_error("porcion_litros debe ser ≥ 1", 400)
            p.porcion_litros = val
        if "slot" in data:
            new_slot = int(data["slot"])
            if new_slot != p.slot_id and \
               Producto.query.filter(Producto.dispenser_id == p.dispenser_id, Producto.slot_id == new_slot, Producto.id != p.id).first():
                return json_error("Slot ya asignado a otro producto en este dispenser", 409)
            p.slot_id = new_slot
        if "habilitado" in data: p.habilitado = bool(data["habilitado"])
        if "bundle_precios" in data: p.bundle_precios = data["bundle_precios"] or {}
        after = int(p.cantidad or 0)
        if after != before: _post_stock_change_hook(p, motivo="update cantidad")
        db.session.commit()
        return ok_json({"ok": True, "producto": serialize_producto(p)})
    except Exception as e:
        db.session.rollback()
        return json_error("Error actualizando producto", 500, str(e))

@app.post("/api/productos/<int:pid>/reponer")
def productos_reponer(pid):
    p = Producto.query.get_or_404(pid)
    litros = _to_int((request.get_json(force=True) or {}).get("litros", 0))
    if litros <= 0: return json_error("Litros inválidos", 400)
    try:
        p.cantidad = max(0, int(p.cantidad or 0) + litros)
        _post_stock_change_hook(p, motivo="reponer")
        db.session.commit()
        return ok_json({"ok": True, "producto": serialize_producto(p)})
    except Exception as e:
        db.session.rollback()
        return json_error("Error reponiendo producto", 500, str(e))

@app.post("/api/productos/<int:pid>/reset_stock")
def productos_reset(pid):
    p = Producto.query.get_or_404(pid)
    litros = _to_int((request.get_json(force=True) or {}).get("litros", 0))
    if litros < 0: return json_error("Litros inválidos", 400)
    try:
        p.cantidad = int(litros)
        _post_stock_change_hook(p, motivo="reset_stock")
        db.session.commit()
        return ok_json({"ok": True, "producto": serialize_producto(p)})
    except Exception as e:
        db.session.rollback()
        return json_error("Error reseteando stock", 500, str(e))

# Opciones 1/2/3 L (bundle)
@app.get("/api/productos/<int:pid>/opciones")
def productos_opciones(pid):
    litros_list = [1, 2, 3]
    try:
        prod = Producto.query.get_or_404(pid)
        disp = Dispenser.query.get(prod.dispenser_id) if prod.dispenser_id else None
        if not prod.habilitado or not disp or not disp.activo:
            return json_error("no_disponible", 400)
        _, reserva = get_thresholds()
        options = []
        for L in litros_list:
            if (int(prod.cantidad) - L) < reserva:
                options.append({"litros": L, "disponible": False})
            else:
                options.append({"litros": L, "disponible": True, "precio_final": compute_total_price_ars(prod, L)})
        return ok_json({"ok": True, "producto": serialize_producto(prod), "opciones": options})
    except Exception as e:
        return json_error("error_opciones", 500, str(e))

# Pricing/bundles
def compute_total_price_ars(prod: Producto, litros: int) -> int:
    litros = int(litros or 1)
    bundles = prod.bundle_precios or {}
    if str(litros) in bundles:
        try:
            return int(round(float(bundles[str(litros)])))
        except Exception:
            pass
    try:
        base = float(prod.precio) * litros
        return int(round(base))
    except Exception:
        return max(1, litros)

# ============ Pagos + MP ============

@app.get("/api/pagos")
def pagos_list():
    try:
        limit = int(request.args.get("limit", 50))
        limit = max(1, min(limit, 200))
    except Exception:
        limit = 50

    estado = (request.args.get("estado") or "").strip()
    qsearch = (request.args.get("q") or "").strip()
    dispenser_id = (request.args.get("dispenser_id") or "").strip()
    desde = request.args.get("desde")
    hasta = request.args.get("hasta")

    q = Pago.query

    # 🔹 Filtro por dispenser (usa el campo correcto)
    if dispenser_id:
        q = q.filter((Pago.dispenser_id == dispenser_id) | (Pago.device_id == dispenser_id))

    # 🔹 Filtro por estado
    if estado:
        q = q.filter(Pago.estado == estado)

    # 🔹 Filtro por texto libre
    if qsearch:
        q = q.filter(Pago.mp_payment_id.ilike(f"%{qsearch}%"))

    # 🔹 Filtros opcionales de fecha
    if desde:
        try:
            desde_dt = datetime.fromisoformat(desde)
            q = q.filter(Pago.created_at >= desde_dt)
        except Exception:
            pass

    if hasta:
        try:
            hasta_dt = datetime.fromisoformat(hasta)
            q = q.filter(Pago.created_at <= hasta_dt)
        except Exception:
            pass

    pagos = q.order_by(Pago.id.desc()).limit(limit).all()

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

@app.post("/api/pagos/preferencia")
def crear_preferencia_api():
    data = request.get_json(force=True, silent=True) or {}
    product_id = _to_int(data.get("product_id") or 0)
    litros = _to_int(data.get("litros") or 0) or 1

    token, _base_api = get_mp_token_and_base()
    if not token: return json_error("MP token no configurado", 500)

    prod = Producto.query.get(product_id)
    if not prod or not prod.habilitado: return json_error("producto no disponible", 400)
    disp = Dispenser.query.get(prod.dispenser_id) if prod.dispenser_id else None
    if not disp or not disp.activo: return json_error("dispenser no disponible", 400)

    _, reserva = get_thresholds()
    if (int(prod.cantidad) - litros) < reserva:
        return json_error("stock_reserva", 409, {"stock": int(prod.cantidad), "reserva": reserva})

    monto_final = compute_total_price_ars(prod, litros)

    backend_base = BACKEND_BASE_URL or request.url_root.rstrip("/")
    external_ref = f"pid={prod.id};slot={prod.slot_id};litros={litros};disp={disp.id};dev={disp.device_id}"
    body = {
        "items": [{
            "id": str(prod.id),
            "title": f"{prod.nombre} · {litros} L",
            "description": prod.nombre,
            "quantity": 1,
            "currency_id": "ARS",
            "unit_price": float(monto_final),
        }],
        "description": f"{prod.nombre} · {litros} L",
        "metadata": {
            "slot_id": int(prod.slot_id),
            "product_id": int(prod.id),
            "producto": prod.nombre,
            "litros": int(litros),
            "dispenser_id": int(disp.id),
            "device_id": disp.device_id,
            "precio_final": int(monto_final),
        },
        "external_reference": external_ref,
        "auto_return": "approved",
        "back_urls": {
            "success": f"{WEB_URL}/gracias?status=success",
            "failure": f"{WEB_URL}/gracias?status=failure",
            "pending": f"{WEB_URL}/gracias?status=pending"
        },
        "notification_url": f"{backend_base}/api/mp/webhook",
        "statement_descriptor": "DISPEN-EASY",
    }

    try:
        r = requests.post(
            "https://api.mercadopago.com/checkout/preferences",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=body, timeout=20
        ); r.raise_for_status()
    except Exception as e:
        detail = getattr(r, "text", str(e))[:600]
        return json_error("mp_preference_failed", 502, detail)

    pref = r.json() or {}
    link = pref.get("init_point") or pref.get("sandbox_init_point")
    if not link: return json_error("preferencia_sin_link", 502, pref)
    return ok_json({"ok": True, "link": link, "raw": pref, "precio_final": monto_final})
# Webhook MP
@app.post("/api/mp/webhook")
def mp_webhook():
    body = request.get_json(silent=True) or {}
    args = request.args or {}
    topic = args.get("topic") or body.get("type")
    live_mode = bool(body.get("live_mode", True))
    base_api = "https://api.mercadopago.com" if live_mode else "https://api.sandbox.mercadopago.com"
    token, _ = get_mp_token_and_base()

    payment_id = None
    if topic == "payment":
        if "resource" in body and isinstance(body["resource"], str):
            try: payment_id = body["resource"].rstrip("/").split("/")[-1]
            except Exception: payment_id = None
        payment_id = payment_id or (body.get("data") or {}).get("id") or args.get("id")
    if not payment_id:
        return "ok", 200

    try:
        r_pay = requests.get(f"{base_api}/v1/payments/{payment_id}",
                             headers={"Authorization": f"Bearer {token}"}, timeout=15)
        r_pay.raise_for_status()
    except Exception:
        return "ok", 200

    pay = r_pay.json() or {}
    estado = (pay.get("status") or "").lower()
    md = pay.get("metadata") or {}
    product_id = _to_int(md.get("product_id") or 0)
    slot_id = _to_int(md.get("slot_id") or 0)
    litros = _to_int(md.get("litros") or 0)
    dispenser_id = _to_int(md.get("dispenser_id") or 0)
    device_id = str(md.get("device_id") or "")
    monto = _to_int(md.get("precio_final") or 0) or _to_int(pay.get("transaction_amount") or 0)

    producto_txt = (md.get("producto") or pay.get("description") or "")[:120]
    p = Pago.query.filter_by(mp_payment_id=str(payment_id)).first()
    if not p:
        p = Pago(
            mp_payment_id=str(payment_id), estado=estado or "pending", producto=producto_txt,
            dispensado=False, procesado=False, slot_id=slot_id, litros=litros if litros>0 else 1,
            monto=monto, product_id=product_id, dispenser_id=dispenser_id, device_id=device_id, raw=pay
        ); db.session.add(p)
    else:
        p.estado = estado or p.estado; p.producto = producto_txt or p.producto
        p.slot_id = p.slot_id or slot_id; p.product_id = p.product_id or product_id
        p.litros = p.litros or (litros if litros>0 else p.litros)
        p.monto = p.monto or monto; p.dispenser_id = p.dispenser_id or dispenser_id
        p.device_id = p.device_id or device_id; p.raw = pay
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return "ok", 200

    try:
        if p.estado == "approved" and not p.dispensado and not getattr(p, "procesado", False) and p.slot_id and p.litros:
            dev = p.device_id
            if not dev and p.product_id:
                pr = Producto.query.get(p.product_id)
                if pr and pr.dispenser_id:
                    d = Dispenser.query.get(pr.dispenser_id)
                    dev = d.device_id if d else ""
            if dev:
                published = send_dispense_cmd(dev, p.mp_payment_id, p.slot_id, p.litros, timeout_s=max(30, p.litros * 5))
                if published:
                    p.procesado = True; db.session.commit()
    except Exception:
        pass
    return "ok", 200

@app.post("/webhook")
def mp_webhook_alias1(): return mp_webhook()
@app.post("/mp/webhook")
def mp_webhook_alias2(): return mp_webhook()

# Estado para Admin (fallback)
@app.get("/api/dispensers/status")
def api_disp_status():
    out = []

    for dev, info in last_status.items():
        # Convertir el nombre del dispenser (ej: "dispen-01") a número entero
        disp_id = None
        if isinstance(dev, str) and dev.startswith("dispen-"):
            try:
                disp_id = int(dev.replace("dispen-", ""))
            except:
                disp_id = None

        # Buscar operador asignado a ese dispenser (si existe)
        op = OperatorToken.query.filter_by(dispenser_id=disp_id).first() if disp_id else None
        nombre_op = op.nombre if op else None

        out.append({
            "device_id": dev,
            "status": info.get("status", "offline"),
            "operator": nombre_op
        })

    return jsonify(out)

# Debug: ver mapa de status que mantiene el backend
@app.get("/api/debug/last_status")
def debug_last_status():
    return ok_json({"last_status": last_status})

# Reset por dispenser
@app.post("/api/admin/reset_dispenser")
def admin_reset_dispenser():
    data = request.get_json(force=True, silent=True) or {}
    did = int(data.get("dispenser_id") or 0)
    mode = (data.get("mode") or "soft").lower()           # "soft" | "hard_keep" | "hard_wipe"
    reset_stock_to = data.get("reset_stock_to", None)
    confirm = (data.get("confirm") or "").strip().lower()

    if not did or confirm != "reset":
        return json_error("dispenser_id y confirm='reset' requeridos", 400)

    disp = Dispenser.query.get(did)
    if not disp:
        return json_error("dispenser no encontrado", 404)

    try:
        deleted_pagos = Pago.query.filter(Pago.dispenser_id == did).delete(synchronize_session=False)

        if mode == "soft":
            stock_set = None
            if reset_stock_to is not None:
                val = max(0, int(reset_stock_to))
                for p in Producto.query.filter(Producto.dispenser_id == did).all():
                    p.cantidad = val
                stock_set = val
            db.session.commit()
            return ok_json({"ok": True, "mode":"soft", "deleted_pagos": deleted_pagos, "stock_set": stock_set})

        if mode == "hard_keep":
            for p in Producto.query.filter(Producto.dispenser_id == did).all():
                p.cantidad = 0
            db.session.commit()
            return ok_json({"ok": True, "mode":"hard_keep", "deleted_pagos": deleted_pagos, "stock_set": 0})

        if mode == "hard_wipe":
            Producto.query.filter(Producto.dispenser_id == did).delete(synchronize_session=False)
            db.session.commit()
            return ok_json({"ok": True, "mode":"hard_wipe", "deleted_pagos": deleted_pagos, "productos_borrados": True})

        return json_error("mode inválido (soft|hard_keep|hard_wipe)", 400)

    except Exception as e:
        db.session.rollback()
        return json_error("reset_failed", 500, str(e))

# Operadores (Admin)
@app.get("/api/admin/operator_tokens")
def admin_operator_list():
    toks = OperatorToken.query.order_by(OperatorToken.created_at.desc()).all()
    return jsonify([{
        "token": t.token, "dispenser_id": t.dispenser_id, "nombre": t.nombre or "",
        "activo": bool(t.activo), "chat_id": t.chat_id or "",
        "created_at": t.created_at.isoformat() if t.created_at else None
    } for t in toks])

# ======================================================
# ===  Crear nuevo token de operador  ==================
# ======================================================
@app.post("/api/admin/operator_tokens")
def create_operator_token():
    require_admin()
    data = request.get_json(force=True, silent=True) or {}
    dispenser_id = data.get("dispenser_id")
    nombre = data.get("nombre", "").strip() or "Operador"
    if not dispenser_id:
        return jsonify({"ok": False, "error": "dispenser_id requerido"}), 400

    try:
        tok = secrets.token_urlsafe(16)
        op = OperatorToken(
            token=tok,
            dispenser_id=dispenser_id,
            nombre=nombre,
            activo=True,
            created_at=datetime.utcnow(),
            chat_id=None
        )
        db.session.add(op)
        db.session.commit()

        return jsonify({
            "ok": True,
            "token": tok,
            "dispenser_id": dispenser_id,
            "nombre": nombre
        })
    except Exception as e:
        db.session.rollback()
        print("Error creando operator token:", e)
        return jsonify({"ok": False, "error": str(e)}), 500
@app.put("/api/admin/operator_tokens/<token>")
def admin_operator_update(token):
    data = request.get_json(force=True, silent=True) or {}
    t = OperatorToken.query.get_or_404(token)
    if "dispenser_id" in data:
        did = _to_int(data["dispenser_id"])
        if did and Dispenser.query.get(did): t.dispenser_id = did
    if "nombre" in data: t.nombre = str(data["nombre"] or "")
    if "activo" in data: t.activo = bool(data["activo"])
    if "chat_id" in data: t.chat_id = str(data["chat_id"] or "")
    db.session.commit()
    return ok_json({"ok": True})

@app.delete("/api/admin/operator_tokens/<token>")
def admin_operator_delete(token):
    t = OperatorToken.query.get_or_404(token)
    db.session.delete(t); db.session.commit()
    return ok_json({"ok": True})

# Operadores (público por token)

def _operator_from_header() -> OperatorToken | None:
    """Devuelve el operador autenticado. Acepta token desde:
    - Header: x-operator-token o X-Operator-Token
    - Parámetro de query: ?token=
    - Campo JSON o form-data: {"token": "..."}
    """
    from flask import request, has_request_context

    if not has_request_context():
        app.logger.warning("[AUTH] Llamada fuera de contexto de request")
        return None

    # 🔍 Log completo de headers
    try:
        app.logger.info(f"[DEBUG] HEADERS COMPLETOS: {dict(request.headers)}")
    except Exception as e:
        app.logger.warning(f"[DEBUG] No se pudieron loguear headers: {e}")

    # Intentar extraer token desde múltiples fuentes
    tok = (
        request.headers.get("x-operator-token")
        or request.headers.get("X-Operator-Token")
        or request.args.get("token")
        or (request.get_json(silent=True) or {}).get("token")
        or request.form.get("token")
        or ""
    ).strip()

    app.logger.info(f"[DEBUG] Token recibido en _operator_from_header: {tok}")

    if not tok:
        return None

    op = OperatorToken.query.filter_by(token=tok, activo=True).first()
    if not op:
        app.logger.warning(f"[AUTH] Token inválido o inactivo: {tok}")
        return None

    return op
    
@app.get("/api/operator/productos")
def operator_productos():
    """Devuelve los productos visibles para el panel del operador"""
    token = request.headers.get("x-operator-token")
    op = OperatorToken.query.filter_by(token=token, activo=True).first()

    if not op:
        return jsonify({"ok": False, "error": "Token inválido o inactivo"}), 401

    # 🔹 Ordenamos por número de slot (como en el gabinete físico)
    productos = (
        Producto.query.filter_by(dispenser_id=op.dispenser_id)
        .order_by(Producto.slot_id.asc())
        .all()
    )

    return jsonify({
        "ok": True,
        "operator": {
            "token": op.token,
            "nombre": getattr(op, "nombre", ""),
            "dispenser_id": op.dispenser_id,
            "chat_id": getattr(op, "chat_id", None),
        },
        "productos": [
            {
                "id": p.id,
                "nombre": p.nombre,
                "slot": p.slot_id,
                "precio": p.precio,
                "cantidad": p.cantidad,
                "habilitado": p.habilitado,
                # ✅ Conserva los bundles definidos
                "bundle2": (p.bundle_precios or {}).get("2"),
                "bundle3": (p.bundle_precios or {}).get("3"),
            }
            for p in productos
        ],
    })

@app.get("/api/operator/estado")
def operator_estado():
    """Devuelve el estado actual del dispenser asociado al operador."""
    token = request.args.get("token") or request.headers.get("x-operator-token", "").strip()
    if not token:
        return jsonify({"ok": False, "error": "Falta token"}), 400

    op = OperatorToken.query.filter_by(token=token, activo=True).first()
    if not op:
        return jsonify({"ok": False, "error": "Operador inválido o inactivo"}), 401

    disp = Dispenser.query.get(op.dispenser_id)
    if not disp:
        return jsonify({"ok": False, "error": "Dispenser no encontrado"}), 404

    return jsonify({
        "ok": True,
        "dispenser": {
            "id": disp.id,
            "device_id": disp.device_id,
            "nombre": disp.nombre,
            "activo": bool(disp.activo)
        },
        "operator": {
            "token": op.token,
            "nombre": op.nombre
        }
    })
# ==========================================
# ✅ REPOENER PRODUCTO (sumar stock)
# ==========================================
# --- REPOSICIÓN DE PRODUCTO POR OPERADOR ---
@app.post("/api/operator/productos/reponer")
def operator_reponer():
    token = request.headers.get("x-operator-token")
    if not token:
        return jsonify({"ok": False, "error": "Falta token"}), 401

    op = OperatorToken.query.filter_by(token=token).first()
    if not op:
        return jsonify({"ok": False, "error": "Token inválido"}), 401

    data = request.get_json(force=True)
    pid = data.get("product_id")
    litros = float(data.get("litros", 0))

    p = Producto.query.filter_by(id=pid, dispenser_id=op.dispenser_id).first()
    if not p:
        return jsonify({"ok": False, "error": "Producto no encontrado"}), 404

    # ✅ sumar litros
    p.cantidad = (p.cantidad or 0) + litros
    if not p.bundle_precios:
        p.bundle_precios = {}

    db.session.commit()
    
    check_stock_alert(p, op)
    
    # ✅ notificar por Telegram si está vinculado
    if op.chat_id:
        try:
            msg = f"📦 Reposición realizada\n\nProducto: {p.nombre}\nCantidad: {litros} L\nStock actual: {p.cantidad} L"
            send_telegram_message(op.chat_id, msg)
        except Exception as e:
            print("Error enviando Telegram:", e)

    return jsonify({"ok": True, "producto": p.to_dict()})

# ==========================================
# ✅ RESETEAR PRODUCTO (setear stock exacto)
# ==========================================
@app.post("/api/operator/productos/reset")
def operator_reset():
    token = request.headers.get("x-operator-token")
    if not token:
        return jsonify({"ok": False, "error": "Falta token"}), 401

    op = OperatorToken.query.filter_by(token=token).first()
    if not op:
        return jsonify({"ok": False, "error": "Token inválido"}), 401

    data = request.get_json(force=True)
    pid = data.get("product_id")
    litros = float(data.get("litros", 0))

    p = Producto.query.filter_by(id=pid, dispenser_id=op.dispenser_id).first()
    if not p:
        return jsonify({"ok": False, "error": "Producto no encontrado"}), 404

    # ✅ set directo
    p.cantidad = litros
    if not p.bundle_precios:
        p.bundle_precios = {}

    db.session.commit()
   
    check_stock_alert(p, op)
    
    # ✅ notificación Telegram
    if op.chat_id:
        try:
            msg = f"🔄 Stock reiniciado\n\nProducto: {p.nombre}\nNuevo stock: {litros} L"
            send_telegram_message(op.chat_id, msg)
        except Exception as e:
            print("Error enviando Telegram:", e)

    return jsonify({"ok": True, "producto": p.to_dict()})
        
@app.post("/api/operator/link")
def operator_link():
    data = request.get_json(force=True, silent=True) or {}
    tok = (data.get("token") or "").strip()
    chat_id = (data.get("chat_id") or "").strip()
    if not tok or not chat_id: return json_error("token y chat_id requeridos", 400)
    t = OperatorToken.query.get(tok)
    if not t: return json_error("token inválido", 404)
    t.chat_id = chat_id
    db.session.commit()
    tg_notify_all(f"🔗 Operador vinculado: '{t.nombre or t.token[:6]}' → dispenser {t.dispenser_id}", dispenser_id=t.dispenser_id)
    return ok_json({"ok": True})

@app.post("/api/operator/unlink")
def operator_unlink():
    """Permite desvincular el chat_id (Telegram) del operador."""
    data = request.get_json(force=True, silent=True) or {}
    tok = (data.get("token") or "").strip()
    if not tok:
        return json_error("token requerido", 400)
    t = OperatorToken.query.get(tok)
    if not t:
        return json_error("token inválido", 404)

    t.chat_id = ""
    db.session.commit()
    tg_notify_all(
        f"❌ Operador desvinculado de Telegram: '{t.nombre or t.token[:6]}' (disp {t.dispenser_id})",
        dispenser_id=t.dispenser_id
    )
    return ok_json({"ok": True})

# Gracias / Sin stock
@app.get("/gracias")
def pagina_gracias():
    status = (request.args.get("status") or "").lower()
    if status in ("success","approved"):
        title="¡Gracias por su compra!"; subtitle='<span class="ok">Pago aprobado.</span> Presione el botón del producto seleccionado para dispensar.'
    elif status in ("pending","in_process"):
        title="Pago pendiente"; subtitle="Tu pago está en revisión."
    else:
        title="Pago no completado"; subtitle='<span class="err">El pago fue cancelado o rechazado. Intente nuevamente.</span>'
    return _html(title, f"<p>{subtitle}</p>")

@app.get("/sin-stock")
def pagina_sin_stock():
    return _html("❌ Producto sin stock", "<p>Este producto alcanzó la reserva crítica.</p>")

def _html(title: str, body_html: str):
    html = f"""<!doctype html><html lang="es"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{title}</title>
</head><body style="background:#0b1220;color:#e5e7eb;font-family:Inter,system-ui,Segoe UI,Roboto">
<div style="max-width:720px;margin:14vh auto;background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.08);border-radius:16px;padding:20px">
<h1 style="margin:0 0 8px">{title}</h1>
{body_html}
</div></body></html>"""
    r = make_response(html, 200); r.headers["Content-Type"]="text/html; charset=utf-8"; return r

# QR de selección
@app.get("/ui/seleccionar")
def ui_seleccionar():
    """Página de selección de litros (1L, 2L, 3L) para generar el pago"""
    pid = request.args.get("pid", type=int)
    op_token = request.args.get("op_token", "").strip()  # 🔹 Nuevo: token del operador si viene del QR

    if not pid:
        return _html("Producto no encontrado", "<p>Falta parámetro <code>pid</code>.</p>")

    prod = Producto.query.get(pid)
    if not prod or not prod.habilitado:
        return _html("No disponible", "<p>Producto sin stock o deshabilitado.</p>")

    disp = Dispenser.query.get(prod.dispenser_id) if prod.dispenser_id else None
    if not disp or not disp.activo:
        return _html("No disponible", "<p>Dispenser no disponible.</p>")

   # 🔧 Cargar precios de bundle_precios (acepta dict o texto JSON)
try:
    if isinstance(prod.bundle_precios, dict):
        precios = prod.bundle_precios
    else:
        precios = json.loads(prod.bundle_precios or "{}")
except Exception as e:
    precios = {}

# Si por alguna razón no hay precios configurados, usar el precio base
precio_base = float(prod.precio or 0)

precio_1 = float(precios.get("1", precio_base))
precio_2 = float(precios.get("2", precio_1 * 2))
precio_3 = float(precios.get("3", precio_1 * 3))
    # ================================
    # 🧭 HTML con los botones de selección
    # ================================
html = f"""
    <html>
    <head>
        <meta charset="utf-8">
        <title>Seleccionar cantidad</title>
        <style>
            body {{
                background-color: #111;
                color: white;
                font-family: Arial, sans-serif;
                text-align: center;
                margin-top: 100px;
            }}
            button {{
                font-size: 22px;
                margin: 10px;
                padding: 15px 30px;
                border: none;
                border-radius: 12px;
                cursor: pointer;
                background-color: #007bff;
                color: white;
            }}
            button:hover {{
                background-color: #0056b3;
            }}
        </style>
    </head>
    <body>
        <h2>{prod.nombre}</h2>
        <p>Seleccioná la cantidad a comprar:</p>

        <button onclick="pagar(1)">1 L — ${precio_1}</button>
        <button onclick="pagar(2)">2 L — ${precio_2}</button>
        <button onclick="pagar(3)">3 L — ${precio_3}</button>

        <script>
        async function pagar(bundle) {{
            const body = {{
                product_id: {pid},
                bundle: bundle,
                op_token: "{op_token}"  // 🔹 Enviamos token del operador
            }};
            try {{
                const resp = await fetch('/api/pagos/preferencia', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify(body)
                }});
                const data = await resp.json();
                if (data.ok && data.url) {{
                    window.location.href = data.url;
                }} else {{
                    alert('Error al generar el pago: ' + (data.error || 'Desconocido'));
                }}
            }} catch (err) {{
                alert('Error de conexión: ' + err);
            }}
        }}
        </script>
    </body>
    </html>
    """

return _html("Seleccionar cantidad", html)
# ======================================================
# ===  Panel de vinculación para operadores  ============
# ======================================================

@app.get("/vincular_mp")
def vincular_mp():
    """Panel simple donde el operador puede vincular su cuenta de MercadoPago."""
    token = request.args.get("token") or request.headers.get("x-operator-token")
    if not token:
        return _html("Vinculación MercadoPago", "<p>Falta token del operador.</p>")

    op = OperatorToken.query.get(token)
    if not op:
        return _html("Error", "<p>Operador no encontrado.</p>")

    # Verificar si ya tiene una cuenta vinculada
    if op.mp_access_token:
        html = f"""
        <h3>Cuenta de MercadoPago ya vinculada ✅</h3>
        <p>Operador: <b>{op.nombre or token[:6]}</b></p>
        <p>Podés desvincularla si es necesario.</p>
        """
    else:
        # Mostrar botón para iniciar OAuth
        auth_url = f"{BACKEND_BASE_URL}/api/mp/oauth_start?token={token}"
        html = f"""
        <h3>Vincular cuenta de MercadoPago</h3>
        <p>Operador: <b>{op.nombre or token[:6]}</b></p>
        <p>Actualmente no hay cuenta vinculada.</p>
        <a href="{auth_url}" style="background:#009EE3;color:white;padding:10px 20px;text-decoration:none;border-radius:8px;">Vincular MercadoPago</a>
        """

    return _html("Vinculación MercadoPago", html)

# =======================
# Ingreso por Token (nuevo)
# =======================

@app.route("/operator_login", methods=["GET", "POST"])
def operator_login():
    if request.method == "POST":
        data = request.get_json(force=True)
        token = (data.get("token") or "").strip()

        op = OperatorToken.query.filter_by(token=token).first()
        if not op:
            return jsonify({"error": "Token inválido"}), 401

        # Redirige al panel del operador correspondiente
        return jsonify({
            "success": True,
            "redirect": f"/operator?token={token}",
            "nombre": op.nombre
        })

    # Si es GET, solo muestra la página HTML de ingreso
    return render_template("operator_login.html")

# =====================
# EDITAR PRODUCTOS (OPERADOR)
# =====================
@app.post("/api/operator/productos/update")
def operator_update_producto():
    """Permite al operador actualizar precios, bundles y habilitación"""
    token = request.headers.get("x-operator-token")
    op = OperatorToken.query.filter_by(token=token, activo=True).first()
    if not op:
        return jsonify({"ok": False, "error": "Token inválido"}), 401

    data = request.get_json() or {}
    pid = data.get("product_id")
    precio = data.get("precio")
    bundle2 = data.get("bundle2")
    bundle3 = data.get("bundle3")
    habilitado = data.get("habilitado")

    p = Producto.query.filter_by(id=pid, dispenser_id=op.dispenser_id).first()
    if not p:
        return jsonify({"ok": False, "error": "Producto no encontrado"}), 404

    # 🔹 Actualizamos los campos recibidos
    if precio is not None:
        try:
            p.precio = float(precio)
        except ValueError:
            pass

    if habilitado is not None:
        p.habilitado = bool(habilitado)

    # 🔹 Actualizamos bundles sin borrar los existentes
    bp = p.bundle_precios or {}
    if bundle2 is not None:
        if str(bundle2).strip() == "":
            bp.pop("2", None)
        else:
            bp["2"] = float(bundle2)
    if bundle3 is not None:
        if str(bundle3).strip() == "":
            bp.pop("3", None)
        else:
            bp["3"] = float(bundle3)
    p.bundle_precios = bp

    db.session.commit()

    # 🔹 Notificación opcional a Telegram
    try:
        chat_id = getattr(op, "chat_id", None)
        if chat_id:
            msg = (
                f"🧴 *Producto actualizado en tu dispenser #{op.dispenser_id}*\n\n"
                f"Nombre: {p.nombre}\n"
                f"Precio: ${p.precio:.2f}/L\n"
                f"Bundle 2L: {bp.get('2', '-')}\n"
                f"Bundle 3L: {bp.get('3', '-')}"
            )
            send_telegram_message(chat_id, msg)
    except Exception as e:
        print("Error enviando mensaje Telegram:", e)

    return jsonify({
        "ok": True,
        "producto": p.to_dict(),
    })

# ===============================
# 📦 Generar link QR desde panel del operador
# ===============================
@app.get("/api/operator/productos/qr/<int:product_id>")
def operator_generar_qr(product_id):
    """Genera un QR con selector de litros, reflejo del Admin pero usando la cuenta MercadoPago del operador"""
    token = request.headers.get("x-operator-token", "").strip()
    if not token:
        return jsonify({"ok": False, "error": "Token requerido"}), 401

    op = OperatorToken.query.filter_by(token=token, activo=True).first()
    if not op:
        return jsonify({"ok": False, "error": "Operador inválido o inactivo"}), 401

    prod = Producto.query.get(product_id)
    if not prod or prod.dispenser_id != op.dispenser_id:
        return jsonify({"ok": False, "error": "Producto no autorizado o inexistente"}), 404

    disp = Dispenser.query.get(prod.dispenser_id)
    if not disp or not disp.activo:
        return jsonify({"ok": False, "error": "Dispenser no disponible"}), 400

    # ✅ Generar link igual que el admin, pero incluyendo el token del operador
    backend_base = BACKEND_BASE_URL or request.url_root.rstrip("/")
    link = f"{backend_base}/ui/seleccionar?pid={product_id}&op_token={token}"

    # (Opcional) generar QR base64 si el frontend quiere mostrar la imagen directamente
    import qrcode
    import io, base64
    qr_buffer = io.BytesIO()
    qrcode.make(link).save(qr_buffer, format="PNG")
    qr_base64 = base64.b64encode(qr_buffer.getvalue()).decode("utf-8")

    return jsonify({
        "ok": True,
        "url": link,
        "qr": qr_base64,
        "producto": {
            "id": prod.id,
            "nombre": prod.nombre,
            "precio": prod.precio,
            "dispenser_id": prod.dispenser_id,
            "dispenser_nombre": disp.nombre
        }
    })
    #-----PANEL CONTABLE-----#

@app.route('/api/contabilidad/resumen', methods=['GET'])
def contabilidad_resumen():
    """
    Devuelve resumen de ventas agrupadas por operador, con totales de monto, litros y comisiones.
    """
    desde = request.args.get('desde')
    hasta = request.args.get('hasta')

    if not desde or not hasta:
        hasta = datetime.now().strftime("%Y-%m-%d")
        desde = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")

    # Leer comisión editable de tabla configuracion
    cfg = Configuracion.query.filter_by(clave='comision_porcentaje').first()
    COMISION_PORCENTAJE = float(cfg.valor) if cfg else 10

    query = text("""
        SELECT 
            operator_token AS operador,
            SUM(monto) AS total_ventas,
            SUM(litros) AS litros_vendidos,
            COUNT(*) AS cantidad_transacciones
        FROM pago
        WHERE estado = 'approved'
          AND fecha BETWEEN :desde AND :hasta
        GROUP BY operator_token
        ORDER BY total_ventas DESC
    """)
    result = db.session.execute(query, {'desde': desde, 'hasta': hasta}).fetchall()

    resumen = []
    for r in result:
        comision = round(r.total_ventas * COMISION_PORCENTAJE / 100, 2)
        neto = round(r.total_ventas - comision, 2)
        resumen.append({
            "operador": r.operador or "Sin asignar",
            "ventas_totales": round(r.total_ventas, 2),
            "litros_vendidos": round(r.litros_vendidos or 0, 2),
            "comision": comision,
            "neto_operador": neto,
            "transacciones": r.cantidad_transacciones
        })

    return ok_json({
        "desde": desde,
        "hasta": hasta,
        "comision_porcentaje": COMISION_PORCENTAJE,
        "resumen": resumen
    })  

@app.route('/api/contabilidad/ranking_productos', methods=['GET'])
def ranking_productos():
    """
    Devuelve ranking de productos más y menos vendidos por monto.
    """
    desde = request.args.get('desde')
    hasta = request.args.get('hasta')

    if not desde or not hasta:
        hasta = datetime.now().strftime("%Y-%m-%d")
        desde = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")

    query = text("""
        SELECT 
            pr.nombre AS producto,
            SUM(pg.litros) AS litros_totales,
            SUM(pg.monto) AS monto_total,
            COUNT(pg.id) AS transacciones
        FROM pago pg
        JOIN producto pr ON pg.id_producto = pr.id
        WHERE pg.estado = 'approved'
          AND pg.fecha BETWEEN :desde AND :hasta
        GROUP BY pr.nombre
        ORDER BY monto_total DESC
    """)
    result = db.session.execute(query, {'desde': desde, 'hasta': hasta}).fetchall()

    data = [
        {
            "producto": r.producto,
            "litros_totales": float(r.litros_totales or 0),
            "monto_total": float(r.monto_total or 0),
            "transacciones": int(r.transacciones)
        }
        for r in result
    ]

    top = data[:5]
    low = list(reversed(data[-5:]))

    return ok_json({
        "desde": desde,
        "hasta": hasta,
        "top": top,
        "low": low
    })
    
# ============ DEBUG ADMIN SECRET ============
@app.get("/api/_debug/admin")
def debug_admin_secret():
    env = _admin_env()
    hdr = _admin_header()
    return jsonify({
        "ok": True,
        "admin_env": env,
        "header_recibido": hdr
    })

@app.get("/api/debug/tokens")
def debug_tokens():
    ops = OperatorToken.query.all()
    return jsonify([
        {"token": o.token, "nombre": (o.nombre or ""), "dispenser_id": o.dispenser_id, "activo": bool(o.activo)}
        for o in ops
    ])

# ===================== ALERTA DE BAJO STOCK =====================

def check_stock_alert(producto, operador):
    """Envia alerta por Telegram si el stock cae por debajo del umbral"""
    try:
        import os
        umbral = float(os.getenv("STOCK_RESERVA_LTS", 2))
        if producto.cantidad <= umbral and operador.chat_id:
            msg = (
                f"⚠️ *Alerta de bajo stock*\n\n"
                f"Dispenser #{producto.dispenser_id}\n"
                f"Producto: {producto.nombre}\n"
                f"Stock actual: {producto.cantidad} L\n\n"
                f"Recomendación: reponer el producto cuanto antes 🧴"
            )
            send_telegram_message(operador.chat_id, msg)
    except Exception as e:
        print("Error al enviar alerta de bajo stock:", e)


# ==========================================
# 📤 Enviar reporte por Telegram (seguro con variables)
# ==========================================
import requests, os

@app.route("/api/enviar_telegram", methods=["POST"])
def enviar_telegram():
    try:
        data = request.get_json(force=True)
        mensaje = data.get("mensaje", "")
        if not mensaje:
            return jsonify({"error": "mensaje vacío"}), 400

        TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
        TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

        if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
            return jsonify({"error": "faltan variables TELEGRAM_*"}), 500

        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": mensaje,
            "parse_mode": "Markdown",
        }

        r = requests.post(url, json=payload, timeout=10)
        if r.status_code == 200:
            return jsonify({"ok": True})
        else:
            print("Error Telegram:", r.text)
            return jsonify({"error": "telegram error"}), 500
    except Exception as e:
        print("Error enviando telegram:", e)
        return jsonify({"error": str(e)}), 500
    
# ============ MQTT init ============

with app.app_context():
    try: start_mqtt_background()
    except Exception: app.logger.exception("[MQTT] error iniciando hilo")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
