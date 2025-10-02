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

# ---------------- App/DB ----------------
app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL or "sqlite:///local.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
CORS(app, resources={r"/api/*": {"origins": "*"}}, allow_headers=["Content-Type", "x-admin-secret"])
db = SQLAlchemy(app)
logging.basicConfig(level=logging.INFO)
app.logger.setLevel(logging.INFO)

# ---------------- Modelos ----------------
class Dispenser(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    device_id = db.Column(db.String(80), nullable=False, unique=True, index=True)
    nombre = db.Column(db.String(100), nullable=True, default="")
    activo = db.Column(db.Boolean, nullable=False, server_default=db.text("true"))

class Producto(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    dispenser_id = db.Column(db.Integer, db.ForeignKey("dispenser.id", ondelete="SET NULL"), nullable=True, index=True)
    nombre = db.Column(db.String(100), nullable=False)
    precio = db.Column(db.Float, nullable=False)  # $/L
    cantidad = db.Column(db.Integer, nullable=False)  # stock en L
    slot_id = db.Column(db.Integer, nullable=False)  # 1..6
    porcion_litros = db.Column(db.Integer, nullable=False, server_default="1")
    habilitado = db.Column(db.Boolean, nullable=False, server_default=db.text("false"))

class Pago(db.Model):
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

with app.app_context():
    db.create_all()
    if Dispenser.query.count() == 0:
        db.session.add(Dispenser(device_id="dispen-01", nombre="dispen-01 (default)", activo=True))
        db.session.commit()

# ---------------- MQTT ----------------
_mqtt_client = None
_mqtt_lock = threading.Lock()
def topic_cmd(device_id: str) -> str: return f"dispen/{device_id}/cmd/dispense"

def start_mqtt_background():
    if not MQTT_HOST:
        app.logger.warning("[MQTT] MQTT_HOST no configurado")
        return
    def _run():
        global _mqtt_client
        _mqtt_client = mqtt.Client(client_id="dispen-backend")
        if MQTT_USER or MQTT_PASS: _mqtt_client.username_pw_set(MQTT_USER, MQTT_PASS)
        if int(MQTT_PORT) == 8883:
            try: _mqtt_client.tls_set()
            except Exception as e: app.logger.error(f"[MQTT] TLS: {e}")
        _mqtt_client.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
        _mqtt_client.loop_forever()
    threading.Thread(target=_run, name="mqtt-thread", daemon=True).start()

def send_dispense_cmd(device_id: str, payment_id: str, slot_id: int, litros: int) -> bool:
    """Publica al ESP32 para dejar el slot en ARMED (esperando botón)"""
    if not MQTT_HOST: return False
    msg = {
        "payment_id": str(payment_id),
        "slot_id": int(slot_id),
        "litros": int(litros or 1)
    }
    payload = _json.dumps(msg, ensure_ascii=False)
    with _mqtt_lock:
        if not _mqtt_client: return False
        t = topic_cmd(device_id)
        info = _mqtt_client.publish(t, payload, qos=1, retain=False)
        return (info.rc == mqtt.MQTT_ERR_SUCCESS)

# ---------------- Endpoints básicos ----------------
@app.get("/")
def health(): return jsonify({"status": "ok"})

@app.get("/api/dispensers")
def dispensers_list():
    ds = Dispenser.query.all()
    return jsonify([{"id": d.id, "device_id": d.device_id, "nombre": d.nombre, "activo": d.activo} for d in ds])

@app.get("/api/productos")
def productos_list():
    disp_id = int(request.args.get("dispenser_id") or 0)
    q = Producto.query
    if disp_id: q = q.filter(Producto.dispenser_id == disp_id)
    return jsonify([{
        "id": p.id, "dispenser_id": p.dispenser_id, "nombre": p.nombre,
        "precio": p.precio, "cantidad": p.cantidad,
        "slot": p.slot_id, "porcion_litros": p.porcion_litros,
        "habilitado": p.habilitado
    } for p in q.all()])

@app.get("/api/pagos")
def pagos_list():
    pagos = Pago.query.order_by(Pago.id.desc()).limit(10).all()
    return jsonify([{
        "id": p.id, "mp_payment_id": p.mp_payment_id, "estado": p.estado,
        "producto": p.producto, "slot_id": p.slot_id, "litros": p.litros,
        "monto": p.monto, "dispensado": p.dispensado, "created_at": str(p.id)
    } for p in pagos])

# ---------------- Webhook MP ----------------
@app.post("/api/mp/webhook")
def mp_webhook():
    body = request.get_json(silent=True) or {}
    topic = (request.args.get("topic") or body.get("type") or "").lower()
    payment_id = None

    if topic == "payment":
        payment_id = (body.get("data") or {}).get("id") or request.args.get("id")

    if not payment_id:
        return "ok", 200

    # Consultar pago en MP
    token = MP_ACCESS_TOKEN_TEST
    try:
        r = requests.get(f"https://api.sandbox.mercadopago.com/v1/payments/{payment_id}",
                         headers={"Authorization": f"Bearer {token}"}, timeout=10)
        r.raise_for_status()
    except Exception:
        return "ok", 200

    pay = r.json() or {}
    estado = (pay.get("status") or "").lower()
    md = pay.get("metadata") or {}
    slot_id = int(md.get("slot_id") or 0)
    litros = int(md.get("litros") or 1)
    device_id = md.get("device_id") or "dispen-01"
    producto_txt = md.get("producto") or ""

    # Guardar en DB
    p = Pago.query.filter_by(mp_payment_id=str(payment_id)).first()
    if not p:
        p = Pago(mp_payment_id=str(payment_id), estado=estado, producto=producto_txt,
                 slot_id=slot_id, litros=litros, device_id=device_id, raw=pay)
        db.session.add(p)
    else:
        p.estado = estado
        p.slot_id = p.slot_id or slot_id
        p.litros = p.litros or litros
        p.device_id = p.device_id or device_id
        p.raw = pay
    db.session.commit()

    # 🚩 Aquí está la diferencia: cuando es approved, no arranca directo
    if estado == "approved" and not p.dispensado and p.slot_id:
        send_dispense_cmd(device_id, p.mp_payment_id, p.slot_id, p.litros)

    return "ok", 200

# ---------------- Init ----------------
with app.app_context():
    try: start_mqtt_background()
    except Exception: app.logger.exception("[MQTT] error iniciando MQTT")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
