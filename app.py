import os
import logging
import threading
import requests
import json as _json
import time
import secrets
from collections import defaultdict
from queue import Queue
from threading import Lock

from flask import Flask, jsonify, request, make_response, Response
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from sqlalchemy import UniqueConstraint, text as sqltext, JSON
import paho.mqtt.client as mqtt

# ---------------- Config ----------------
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

ADMIN_SECRET = os.getenv("ADMIN_SECRET", "").strip()

UMBRAL_ALERTA_LTS = int(os.getenv("UMBRAL_ALERTA_LTS", "3") or 3)
STOCK_RESERVA_LTS = int(os.getenv("STOCK_RESERVA_LTS", "1") or 1)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# ---------------- App/DB ----------------
app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL or "sqlite:///local.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

CORS(
    app,
    resources={r"/api/*": {"origins": "*"}},
    allow_headers=["Content-Type", "x-admin-secret", "x-operator-token"],
    expose_headers=["Content-Type"],
)

db = SQLAlchemy(app)
logging.basicConfig(level=logging.INFO)
app.logger.setLevel(logging.INFO)

# ---------------- Modelos ----------------
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
    bundle_precios = db.Column(JSON, nullable=True)       # {"2": 1800, "3": 2600}
    habilitado = db.Column(db.Boolean, nullable=False, server_default=db.text("false"))
    created_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now(), nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), onupdate=db.func.now())
    __table_args__ = (UniqueConstraint("dispenser_id", "slot_id", name="uq_disp_slot"),)

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
    raw = db.Column(JSON, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now(), nullable=False)

class OperatorToken(db.Model):
    __tablename__ = "operator_token"
    token = db.Column(db.String(80), primary_key=True)
    dispenser_id = db.Column(db.Integer, db.ForeignKey("dispenser.id", ondelete="CASCADE"), nullable=False, index=True)
    nombre = db.Column(db.String(100), nullable=True, default="")
    activo = db.Column(db.Boolean, nullable=False, server_default=db.text("true"))
    created_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now(), nullable=False)

with app.app_context():
    db.create_all()
    if not KV.query.get("mp_mode"):
        db.session.add(KV(key="mp_mode", value="test"))
        db.session.commit()
    if Dispenser.query.count() == 0:
        db.session.add(Dispenser(device_id="dispen-01", nombre="dispen-01 (por defecto)", activo=True))
        db.session.commit()
    # Migración defensiva JSONB->JSON (ignorada en SQLite)
    try:
        db.session.execute(sqltext("""
            DO $$
            BEGIN
                IF to_regclass('public.producto') IS NOT NULL THEN
                    ALTER TABLE producto ALTER COLUMN bundle_precios TYPE JSON USING bundle_precios::json;
                END IF;
                IF to_regclass('public.pago') IS NOT NULL THEN
                    ALTER TABLE pago ALTER COLUMN raw TYPE JSON USING raw::json;
                END IF;
            EXCEPTION WHEN others THEN
                NULL;
            END;$$;
        """))
        db.session.commit()
    except Exception:
        db.session.rollback()

# ---------------- Helpers ----------------
def ok_json(data, status=200): return jsonify(data), status
def json_error(msg, status=400, extra=None):
    p={"error":msg}
    if extra is not None: p["detail"]=extra
    return jsonify(p), status

def _to_int(x, default=0):
    try: return int(x)
    except Exception:
        try: return int(float(x))
        except Exception: return default

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
    return {
        "id": d.id, "device_id": d.device_id, "nombre": d.nombre or "",
        "activo": bool(d.activo),
        "created_at": d.created_at.isoformat() if d.created_at else None,
    }

def get_thresholds():
    reserva = max(0, int(STOCK_RESERVA_LTS))
    umbral_cfg = max(0, int(UMBRAL_ALERTA_LTS))
    umbral = umbral_cfg if umbral_cfg > reserva else (reserva + 1)
    return umbral, reserva

# ---- Telegram ----
def tg_notify(text: str):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=10
        )
    except Exception as e:
        app.logger.error(f"[TG] Error enviando notificación: {e}")

def _post_stock_change_hook(prod: "Producto", motivo: str):
    umbral, reserva = get_thresholds()
    stock = int(prod.cantidad or 0)

    if stock <= umbral:
        tg_notify(
            f"⚠️ Bajo stock '{prod.nombre}' (disp {prod.dispenser_id}, slot {prod.slot_id}): "
            f"{stock} L (umbral={umbral}, reserva={reserva}) – {motivo}"
        )

    if stock < reserva:
        if prod.habilitado:
            prod.habilitado = False
            app.logger.info(f"[STOCK] Deshabilitado '{prod.nombre}' disp={prod.dispenser_id} (stock={stock} < {reserva})")
    else:
        if not prod.habilitado:
            prod.habilitado = True
            app.logger.info(f"[STOCK] Re-habilitado '{prod.nombre}' disp={prod.dispenser_id} (stock={stock} ≥ {reserva})")

# --- Pricing ---
def compute_total_price_ars(prod: Producto, litros: int) -> int:
    litros = int(litros or 1)
    bundles = prod.bundle_precios or {}
    if str(litros) in bundles:
        try: return int(round(float(bundles[str(litros)])))
        except Exception: pass
    try: return int(round(float(prod.precio) * litros))
    except Exception: return max(1, litros)

# ---------------- Auth guard ----------------
PUBLIC_PATHS = {
    "/", "/gracias", "/sin-stock",
    "/api/mp/webhook", "/webhook", "/mp/webhook",
    "/api/pagos/preferencia", "/api/pagos/pendiente",
    "/api/config", "/go", "/ui/seleccionar",
    "/api/productos/opciones",
}
@app.before_request
def _auth_guard():
    if request.method == "OPTIONS":
        return "", 200
    p = request.path or ""
    # Permitir rutas públicas y rutas de operador sin admin secret
    if p in PUBLIC_PATHS or (p.startswith("/api/productos/") and p.endswith("/opciones")) or p.startswith("/api/op/"):
        return None
    if not ADMIN_SECRET:
        return None
    if request.headers.get("x-admin-secret") != ADMIN_SECRET:
        return json_error("unauthorized", 401)
    return None

# ---- Operator helpers ----
def _unauth_op():
    return json_error("operator_unauthorized", 401)

def get_operator_scope():
    tok = (request.headers.get("x-operator-token") or "").strip()
    if not tok:
        return None
    return OperatorToken.query.filter_by(token=tok, activo=True).first()

# ---------------- MP tokens ----------------
def get_mp_mode() -> str:
    row = KV.query.get("mp_mode")
    return (row.value if row else "test").lower()

def get_mp_token_and_base() -> tuple[str, str]:
    mode = get_mp_mode()
    if mode == "live":
        return MP_ACCESS_TOKEN_LIVE, "https://api.mercadopago.com"
    return MP_ACCESS_TOKEN_TEST, "https://api.sandbox.mercadopago.com"

# ---------------- MQTT ----------------
_mqtt_client = None
_mqtt_lock = threading.Lock()
def topic_cmd(device_id: str) -> str: return f"dispen/{device_id}/cmd/dispense"
def topic_state_wild() -> str: return "dispen/+/state/dispense"
def topic_status_wild() -> str: return "dispen/+/status"
def topic_event_wild() -> str: return "dispen/+/event"

# ---- SSE infra ----
_sse_clients = []
_sse_lock = Lock()
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
    if ADMIN_SECRET and (request.args.get("secret") or "") != ADMIN_SECRET:
        return json_error("unauthorized", 401)
    q = Queue(maxsize=100)
    with _sse_lock:
        _sse_clients.append(q)
    def gen():
        yield "retry: 5000\n\n"
        try:
            while True:
                data = q.get()
                yield f"data: {_json.dumps(data, ensure_ascii=False)}\n\n"
        except GeneratorExit:
            with _sse_lock:
                try: _sse_clients.remove(q)
                except Exception: pass
    return Response(gen(), mimetype="text/event-stream")

# ---- Estado online/offline ----
last_status = defaultdict(lambda: {"status": "unknown", "t": 0})
_last_notified_status = defaultdict(lambda: "")
OFF_DEBOUNCE_S = 5
ON_DEBOUNCE_S  = 5
_online_timers = {}  # dev -> threading.Timer

def _notify_online_if_stable(dev: str):
    st = (last_status.get(dev) or {}).get("status", "")
    if st == "online" and _last_notified_status[dev] != "online":
        tg_notify(f"✅ {dev}: ONLINE")
        _last_notified_status[dev] = "online"

# ---- MQTT callbacks ----
def _mqtt_on_connect(client, userdata, flags, rc, props=None):
    app.logger.info(f"[MQTT] conectado rc={rc}; subscribe {topic_state_wild()} {topic_status_wild()} {topic_event_wild()}")
    client.subscribe(topic_state_wild(), qos=1)
    client.subscribe(topic_status_wild(), qos=1)
    client.subscribe(topic_event_wild(), qos=1)

def _mqtt_on_message(client, userdata, msg):
    try: raw = msg.payload.decode("utf-8", "ignore")
    except Exception: raw = "<binario>"
    app.logger.info(f"[MQTT] RX topic={msg.topic} payload={raw}")

    # Evento botón físico → SSE
    if msg.topic.startswith("dispen/") and msg.topic.endswith("/event"):
        try: data = _json.loads(raw or "{}")
        except Exception: data = {}
        if str(data.get("event") or "") == "button_press":
            try: dev = msg.topic.split("/")[1]
            except Exception: dev = ""
            slot = int(data.get("slot") or 0)
            _sse_broadcast({"type":"button_press","device_id":dev,"slot":slot})
        return

    # Estado ONLINE/OFFLINE con debounce por Timer
    if msg.topic.startswith("dispen/") and msg.topic.endswith("/status"):
        try:
            data = _json.loads(raw or "{}")
        except Exception:
            return
        dev = str(data.get("device") or "").strip()
        st  = str(data.get("status") or "").lower().strip()
        now = time.time()

        last_status[dev] = {"status": st, "t": now}
        _sse_broadcast({"type": "device_status", "device_id": dev, "status": st})

        if _last_notified_status[dev] == st:
            return

        if st == "offline":
            t = _online_timers.pop(dev, None)
            if t:
                try: t.cancel()
                except Exception: pass
            tg_notify(f"⚠️ {dev}: OFFLINE")
            _last_notified_status[dev] = "offline"
            return

        t = _online_timers.pop(dev, None)
        if t:
            try: t.cancel()
            except Exception: pass
        new_t = threading.Timer(ON_DEBOUNCE_S, _notify_online_if_stable, args=(dev,))
        _online_timers[dev] = new_t
        new_t.daemon = True
        new_t.start()
        return

    # Estado de dispensa DONE/TIMEOUT para stock
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

def send_dispense_cmd(device_id: str, payment_id: str, slot_id: int, litros: int, timeout_s: int = 30) -> bool:
    if not MQTT_HOST: return False
    msg = { "payment_id": str(payment_id), "slot_id": int(slot_id), "litros": int(litros or 1), "timeout_s": int(timeout_s or 30) }
    payload = _json.dumps(msg, ensure_ascii=False)
    with _mqtt_lock:
        if not _mqtt_client: return False
        t = topic_cmd(device_id)
        info = _mqtt_client.publish(t, payload, qos=1, retain=False)
        return (info.rc == mqtt.MQTT_ERR_SUCCESS)

# ---------------- Health ----------------
@app.get("/")
def health(): return ok_json({"status": "ok"})

# ---------------- Config ----------------
@app.get("/api/config")
def api_get_config():
    umbral, reserva = get_thresholds()
    return ok_json({ "mp_mode": get_mp_mode(), "umbral_alerta_lts": umbral, "stock_reserva_lts": reserva })

@app.post("/api/mp/mode")
def api_set_mode():
    data = request.get_json(force=True, silent=True) or {}
    mode = str(data.get("mode") or "").lower()
    if mode not in ("test","live"): return json_error("modo inválido (test|live)", 400)
    kv = KV.query.get("mp_mode") or KV(key="mp_mode", value=mode); kv.value = mode
    db.session.merge(kv); db.session.commit()
    return ok_json({"ok": True, "mp_mode": mode})

# ---------------- Dispensers ----------------
@app.get("/api/dispensers")
def dispensers_list():
    ds = Dispenser.query.order_by(Dispenser.id.asc()).all()
    return jsonify([serialize_dispenser(d) for d in ds])

@app.put("/api/dispensers/<int:did>")
def dispensers_update(did):
    d = Dispenser.query.get_or_404(did)
    data = request.get_json(force=True, silent=True) or {}
    if "nombre" in data: d.nombre = str(data["nombre"]).strip()
    if "activo" in data: d.activo = bool(data["activo"])
    if "device_id" in data:
        nid = str(data["device_id"]).strip()
        if nid and nid != d.device_id:
            if Dispenser.query.filter(Dispenser.device_id == nid, Dispenser.id != d.id).first():
                return json_error("device_id ya usado", 409)
            d.device_id = nid
    db.session.commit()
    return ok_json({"ok": True, "dispenser": serialize_dispenser(d)})

# ---------------- Productos CRUD ----------------
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
            precio=float(data.get("precio", 0)),  # $/L
            cantidad=int(float(data.get("cantidad", 0))),
            slot_id=int(data.get("slot", 1)),
            porcion_litros=int(data.get("porcion_litros", 1)),
            habilitado=bool(data.get("habilitado", False)),
            bundle_precios=data.get("bundle_precios") or {},
        )
        if p.precio < 0 or p.cantidad < 0 or p.porcion_litros < 1:
            return json_error("Valores inválidos", 400)
        if Producto.query.filter(Producto.dispenser_id == p.dispenser_id,
                                 Producto.slot_id == p.slot_id).first():
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
                if Producto.query.filter(Producto.dispenser_id == new_d,
                                         Producto.slot_id == p.slot_id).first():
                    return json_error("Slot ya usado en el nuevo dispenser", 409)
                p.dispenser_id = new_d
        if "nombre" in data: p.nombre = str(data["nombre"]).strip()
        if "precio" in data: p.precio = float(data["precio"])
        if "cantidad" in data:
            new_cant = int(float(data["cantidad"]))
            if new_cant < 0: return json_error("cantidad no puede ser negativa", 400)
            p.cantidad = new_cant
        if "porcion_litros" in data:
            val = int(data["porcion_litros"])
            if val < 1: return json_error("porcion_litros debe ser ≥ 1", 400)
            p.porcion_litros = val
        if "slot" in data:
            new_slot = int(data["slot"])
            if new_slot != p.slot_id and \
               Producto.query.filter(Producto.dispenser_id == p.dispenser_id,
                                     Producto.slot_id == new_slot,
                                     Producto.id != p.id).first():
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

# -------- Opciones (1/2/3 L con precios calculados) --------
@app.get("/api/productos/<int:pid>/opciones")
def productos_opciones(pid):
    litros_list = [1,2,3]
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
                options.append({
                    "litros": L,
                    "disponible": True,
                    "precio_final": compute_total_price_ars(prod, L)
                })
        return ok_json({"ok": True, "producto": serialize_producto(prod), "opciones": options})
    except Exception as e:
        return json_error("error_opciones", 500, str(e))

# ---------------- Pagos ----------------
@app.get("/api/pagos")
def pagos_list():
    try:
        limit = int(request.args.get("limit", 50)); limit = max(1, min(limit, 200))
    except Exception: limit = 50
    estado = (request.args.get("estado") or "").strip()
    qsearch = (request.args.get("q") or "").strip()
    q = Pago.query
    if estado: q = q.filter(Pago.estado == estado)
    if qsearch: q = q.filter(Pago.mp_payment_id.ilike(f"%{qsearch}%"))
    pagos = q.order_by(Pago.id.desc()).limit(limit).all()
    return jsonify([{
        "id": p.id, "mp_payment_id": p.mp_payment_id, "estado": p.estado,
        "producto": p.producto, "product_id": p.product_id,
        "dispenser_id": p.dispenser_id, "device_id": p.device_id,
        "slot_id": p.slot_id, "litros": p.litros, "monto": p.monto,
        "dispensado": bool(p.dispensado),
        "created_at": p.created_at.isoformat() if p.created_at else None,
    } for p in pagos])

# -------------- preferencia (con litros elegidos) --------------
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

# ---------------- Webhook MP ----------------
@app.post("/api/mp/webhook")
def mp_webhook():
    body = request.get_json(silent=True) or {}
    args = request.args or {}

    token, base_api = get_mp_token_and_base()
    topic = args.get("topic") or body.get("type")

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
        db.session.rollback(); 
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

# ---------------- Reintento de dispensa (admin) ----------------
@app.post("/api/pagos/<int:pid>/reenviar")
def pagos_reenviar(pid):
    p = Pago.query.get_or_404(pid)
    if p.dispensado:
        return json_error("ya_dispensado", 409)
    if (p.estado or "").lower() != "approved":
        return json_error("estado_no_aprobado", 409, {"estado": p.estado})
    if not p.slot_id or not p.litros:
        return json_error("pago_incompleto", 400)
    dev = (p.device_id or "").strip()
    if not dev and p.product_id:
        pr = Producto.query.get(p.product_id)
        if pr and pr.dispenser_id:
            d = Dispenser.query.get(pr.dispenser_id)
            if d:
                dev = d.device_id or ""
    if not dev:
        return json_error("sin_device_id", 400, "No se pudo inferir device_id")
    try:
        published = send_dispense_cmd(dev, p.mp_payment_id, int(p.slot_id), int(p.litros), timeout_s=max(30, int(p.litros) * 5))
        if not published:
            return json_error("mqtt_publish_failed", 502)
        p.procesado = True
        db.session.commit()
        return ok_json({"ok": True, "msg": "Comando de dispensa re-enviado", "device_id": dev})
    except Exception as e:
        db.session.rollback()
        return json_error("reenviar_failed", 500, str(e))

# ---------------- Estado para Admin (fallback) ----------------
@app.get("/api/dispensers/status")
def api_disp_status():
    out = []
    for dev, info in last_status.items():
        out.append({"device_id": dev, "status": info["status"]})
    return jsonify(out)

# ---------------- Admin: Operator Tokens ----------------
@app.get("/api/admin/operator_tokens")
def admin_list_operator_tokens():
    rows = OperatorToken.query.order_by(OperatorToken.created_at.desc()).all()
    return ok_json({"ok": True, "tokens": [{
        "token": r.token, "dispenser_id": r.dispenser_id, "nombre": r.nombre or "",
        "activo": bool(r.activo),
        "created_at": r.created_at.isoformat() if r.created_at else None,
    } for r in rows]})

@app.post("/api/admin/operator_tokens")
def admin_create_operator_token():
    data = request.get_json(force=True, silent=True) or {}
    did = int(data.get("dispenser_id") or 0)
    nombre = (data.get("nombre") or "").strip()
    if not did or not Dispenser.query.get(did):
        return json_error("dispenser_id inválido", 400)
    tok = secrets.token_urlsafe(32)
    row = OperatorToken(token=tok, dispenser_id=did, nombre=nombre, activo=True)
    db.session.add(row); db.session.commit()
    return ok_json({"ok": True, "token": tok, "dispenser_id": did, "nombre": nombre})

@app.post("/api/admin/operator_tokens/<string:token>/toggle")
def admin_toggle_operator_token(token):
    row = OperatorToken.query.get_or_404(token)
    row.activo = not bool(row.activo)
    db.session.commit()
    return ok_json({"ok": True, "token": row.token, "activo": bool(row.activo)})

@app.delete("/api/admin/operator_tokens/<string:token>")
def admin_delete_operator_token(token):
    row = OperatorToken.query.get_or_404(token)
    db.session.delete(row); db.session.commit()
    return ok_json({"ok": True, "deleted": True})

# ---------------- Operator (scoped) ----------------
@app.get("/api/op/productos")
def op_list_productos_of_dispenser():
    op = get_operator_scope()
    if not op: return _unauth_op()
    prods = Producto.query.filter(Producto.dispenser_id == op.dispenser_id).order_by(Producto.slot_id.asc()).all()
    return jsonify([serialize_producto(p) for p in prods])

@app.post("/api/op/productos/<int:pid>/reponer")
def op_reponer(pid):
    op = get_operator_scope()
    if not op: return _unauth_op()
    p = Producto.query.get_or_404(pid)
    if p.dispenser_id != op.dispenser_id:
        return json_error("forbidden", 403, "No podés modificar productos de otro dispenser")
    litros = _to_int((request.get_json(force=True) or {}).get("litros", 0))
    if litros <= 0: return json_error("Litros inválidos", 400)
    try:
        p.cantidad = max(0, int(p.cantidad or 0) + litros)
        _post_stock_change_hook(p, motivo=f"reponer (operator:{op.token[:8]}..)")
        db.session.commit()
        return ok_json({"ok": True, "producto": serialize_producto(p)})
    except Exception as e:
        db.session.rollback()
        return json_error("Error reponiendo producto", 500, str(e))

@app.post("/api/op/productos/<int:pid>/reset_stock")
def op_reset_stock(pid):
    op = get_operator_scope()
    if not op: return _unauth_op()
    p = Producto.query.get_or_404(pid)
    if p.dispenser_id != op.dispenser_id:
        return json_error("forbidden", 403, "No podés modificar productos de otro dispenser")
    litros = _to_int((request.get_json(force=True) or {}).get("litros", 0))
    if litros < 0: return json_error("Litros inválidos", 400)
    try:
        p.cantidad = int(litros)
        _post_stock_change_hook(p, motivo=f"reset_stock (operator:{op.token[:8]}..)")
        db.session.commit()
        return ok_json({"ok": True, "producto": serialize_producto(p)})
    except Exception as e:
        db.session.rollback()
        return json_error("Error reseteando stock", 500, str(e))

# ---------------- Gracias / Sin stock ----------------
@app.get("/gracias")
def pagina_gracias():
    status = (request.args.get("status") or "").lower()
    if status in ("success","approved"):
        title="¡Gracias por su compra!"; subtitle='<span class="ok">Pago aprobado.</span> Presione el botón del producto seleccionado para dispensar.'
    elif status in ("pending","in_process"):
        title="Pago pendiente"; subtitle="Tu pago está en revisión."
    else:
        title="Pago no completado"; subtitle='<span class="err">El pago fue cancelado o rechazado.Intente nuevamente.</span>'
    return _html(title, f"<p>{subtitle}</p>")

@app.get("/sin-stock")
def pagina_sin_stock():
    return _html("❌ Producto sin stock", "<p>Este producto alcanzó la reserva crítica.</p>")

# ---- HTML helpers ----
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

def _html_raw(html: str):
    r = make_response(html, 200); r.headers["Content-Type"]="text/html; charset=utf-8"; return r

# ---------------- Inicializar MQTT ----------------
with app.app_context():
    try: start_mqtt_background()
    except Exception: app.logger.exception("[MQTT] error iniciando hilo")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
