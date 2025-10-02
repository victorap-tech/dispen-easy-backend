# app.py
import os
import logging
import threading
import requests
import json as _json

from flask import Flask, jsonify, request, make_response
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy import UniqueConstraint, text as sqltext
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

# ---------------- App/DB ----------------
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
    precio = db.Column(db.Float, nullable=False)   # $/L
    cantidad = db.Column(db.Integer, nullable=False)   # stock en litros
    slot_id = db.Column(db.Integer, nullable=False)    # 1..6
    porcion_litros = db.Column(db.Integer, nullable=False, server_default="1")
    bundle_precios = db.Column(JSONB, nullable=True)
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
    raw = db.Column(JSONB, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now(), nullable=False)

with app.app_context():
    db.create_all()
    if not KV.query.get("mp_mode"):
        db.session.add(KV(key="mp_mode", value="test"))
        db.session.commit()
    if Dispenser.query.count() == 0:
        db.session.add(Dispenser(device_id="dispen-01", nombre="dispen-01 (por defecto)", activo=True))
        db.session.commit()
    try:
        db.session.execute(sqltext("ALTER TABLE producto ADD COLUMN IF NOT EXISTS bundle_precios JSONB"))
        db.session.commit()
    except Exception:
        db.session.rollback()

# ---------------- Helpers ----------------
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

# ---------------- MQTT ----------------
_mqtt_client = None
_mqtt_lock = threading.Lock()
def topic_cmd(device_id: str) -> str: return f"dispen/{device_id}/cmd/dispense"
def topic_state_wild() -> str: return "dispen/+/state/dispense"
def topic_status_wild() -> str: return "dispen/+/status"

def _mqtt_on_connect(client, userdata, flags, rc, props=None):
    app.logger.info(f"[MQTT] conectado rc={rc}; subscribe {topic_state_wild()} y {topic_status_wild()}")
    client.subscribe(topic_state_wild(), qos=1)
    client.subscribe(topic_status_wild(), qos=1)

def _mqtt_on_message(client, userdata, msg):
    try: raw = msg.payload.decode("utf-8", "ignore")
    except Exception: raw = "<binario>"
    app.logger.info(f"[MQTT] RX topic={msg.topic} payload={raw}")
    try: data = _json.loads(raw or "{}")
    except Exception: return
    payment_id = str(data.get("payment_id") or "").strip()
    status = str(data.get("status") or "").lower()
    if status in ("done", "error", "timeout"):
        with app.app_context():
            p = Pago.query.filter_by(mp_payment_id=payment_id).first()
            if p and status == "done" and not p.dispensado:
                try:
                    litros_desc = int(p.litros or 0) or 1
                    prod = Producto.query.get(p.product_id) if p.product_id else None
                    if prod:
                        prod.cantidad = max(0, int(prod.cantidad or 0) - litros_desc)
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

# ---------------- Endpoint Reintento ----------------
@app.post("/api/pagos/<int:pid>/reenviar")
def pagos_reenviar(pid):
    """Permite reenviar un comando de dispensado (ej: si el cliente no presionó botón).
       Usa ?force=true para ignorar 'procesado' y 'dispensado'."""
    force = str(request.args.get("force") or "").lower() in ("1","true","yes")
    p = Pago.query.get_or_404(pid)
    if not force and (p.procesado or p.dispensado):
        return json_error("ya procesado/dispensado", 409)
    if not p.slot_id or not p.device_id:
        return json_error("pago sin device/slot válido", 400)
    ok = send_dispense_cmd(p.device_id, p.mp_payment_id, p.slot_id, p.litros, timeout_s=max(30, p.litros*5))
    if ok:
        p.procesado = True
        db.session.commit()
        return ok_json({"ok": True, "reenviado": True})
    return json_error("falló envío MQTT", 500)

# ---------------- Inicializar MQTT ----------------
with app.app_context():
    try: start_mqtt_background()
    except Exception: app.logger.exception("[MQTT] error iniciando hilo")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
