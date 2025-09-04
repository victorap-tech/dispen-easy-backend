import os
import logging
import threading
import requests
import json as _json
from datetime import datetime

from flask import Flask, jsonify, request
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from sqlalchemy.dialects.postgresql import JSONB
from flask_cors import CORS
import paho.mqtt.client as mqtt

# -------------------------------------------------------------
# Config
# -------------------------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL", "")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "").strip()
BACKEND_BASE_URL = (os.getenv("BACKEND_BASE_URL", "") or "").rstrip("/")
WEB_URL = os.getenv("WEB_URL", "https://example.com").strip()

MQTT_HOST = os.getenv("MQTT_HOST", "").strip()
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883") or 1883)
MQTT_USER = os.getenv("MQTT_USER", "")
MQTT_PASS = os.getenv("MQTT_PASS", "")
DEVICE_ID = os.getenv("DEVICE_ID", "dispen-01").strip()

# 🔒 Seguridad Admin
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "").strip()
PUBLIC_PATHS = {
    "/",                        # health
    "/api/mp/webhook",          # MercadoPago webhook
    "/api/pagos/preferencia",   # generar link/QR
    "/api/pagos/pendiente",     # consulta del ESP32
}

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

# -------------------------------------------------------------
# Seguridad: Admin Secret
# -------------------------------------------------------------
@app.before_request
def guard_admin_secret():
    if not ADMIN_SECRET:
        return  # no bloquea si no está configurado (modo dev)
    path = request.path or ""
    if not path.startswith("/api/"):
        return
    if path in PUBLIC_PATHS:
        return
    key = (request.headers.get("X-Admin-Secret") or "").strip()
    if key != ADMIN_SECRET:
        return jsonify({"error": "unauthorized"}), 401

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
    porcion_litros = db.Column(db.Integer, nullable=False, server_default="1")
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
    procesado = db.Column(db.Boolean, nullable=False, server_default=db.text("false"))
    slot_id = db.Column(db.Integer, nullable=False, default=0)
    litros = db.Column(db.Integer, nullable=False, default=1)
    monto = db.Column(db.Integer, nullable=False, default=0)
    product_id = db.Column(db.Integer, nullable=False, default=0)
    raw = db.Column(JSONB, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now(), nullable=False)

with app.app_context():
    db.create_all()

# -------------------------------------------------------------
# Helpers
# -------------------------------------------------------------
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

def serialize_producto(p: Producto) -> dict:
    return {
        "id": p.id,
        "nombre": p.nombre,
        "precio": float(p.precio),
        "cantidad": int(p.cantidad),
        "slot": int(p.slot_id),
        "porcion_litros": int(getattr(p, "porcion_litros", 1) or 1),
        "habilitado": bool(p.habilitado),
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }

# -------------------------------------------------------------
# MQTT
# -------------------------------------------------------------
MQTT_TOPIC_CMD    = f"dispen/{DEVICE_ID}/cmd/dispense"
MQTT_TOPIC_STATE  = f"dispen/{DEVICE_ID}/state/dispense"
MQTT_TOPIC_STATUS = f"dispen/{DEVICE_ID}/status"
_mqtt_client = None
_mqtt_lock = threading.Lock()

def _mqtt_on_connect(client, userdata, flags, rc, props=None):
    app.logger.info(f"[MQTT] conectado rc={rc}; subscribe {MQTT_TOPIC_STATE} y {MQTT_TOPIC_STATUS}")
    client.subscribe(MQTT_TOPIC_STATE, qos=1)
    client.subscribe(MQTT_TOPIC_STATUS, qos=1)

def _mqtt_on_message(client, userdata, msg):
    try:
        raw = msg.payload.decode("utf-8", "ignore")
    except Exception:
        raw = "<binario>"
    app.logger.info(f"[MQTT] RX topic={msg.topic} payload={raw}")

    try:
        data = _json.loads(raw or "{}")
    except Exception:
        app.logger.exception("[MQTT] payload inválido (no JSON)")
        return

    payment_id = str(data.get("payment_id") or data.get("paymentId") or data.get("id") or "").strip()
    status = str(data.get("status") or data.get("state") or "").lower()
    slot_id = _to_int(data.get("slot_id") or data.get("slot") or 0)
    litros  = _to_int(data.get("litros") or data.get("liters") or 0)

    if status in ("ok", "finish", "finished", "success"):
        status = "done"

    if not payment_id or status not in ("done", "error", "timeout"):
        return

    with app.app_context():
        p = Pago.query.filter_by(mp_payment_id=payment_id).first()
        if not p:
            return
        if status == "done" and not p.dispensado:
            try:
                litros_desc = int(p.litros or 0) or (litros or 1)
                prod = Producto.query.get(p.product_id) if p.product_id else None
                if prod:
                    prod.cantidad = max(0, int(prod.cantidad or 0) - litros_desc)
                p.dispensado = True
                db.session.commit()
            except Exception:
                db.session.rollback()

def start_mqtt_background():
    if not MQTT_HOST:
        return
    def _run():
        global _mqtt_client
        _mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                   client_id=f"{DEVICE_ID}-backend")
        if MQTT_USER or MQTT_PASS:
            _mqtt_client.username_pw_set(MQTT_USER, MQTT_PASS)
        if int(MQTT_PORT) == 8883:
            try:
                _mqtt_client.tls_set()
            except Exception:
                pass
        _mqtt_client.on_connect = _mqtt_on_connect
        _mqtt_client.on_message = _mqtt_on_message
        _mqtt_client.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
        _mqtt_client.loop_forever()
    t = threading.Thread(target=_run, name="mqtt-thread", daemon=True)
    t.start()

def send_dispense_cmd(payment_id: str, slot_id: int, litros: int, timeout_s: int = 30) -> bool:
    if not MQTT_HOST:
        return False
    msg = {
        "payment_id": str(payment_id),
        "slot_id": int(slot_id),
        "litros": int(litros or 1),
        "timeout_s": int(timeout_s or 30),
    }
    payload = _json.dumps(msg, ensure_ascii=False)
    with _mqtt_lock:
        if not _mqtt_client:
            return False
        info = _mqtt_client.publish(MQTT_TOPIC_CMD, payload, qos=1, retain=False)
        return info.rc == mqtt.MQTT_ERR_SUCCESS

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
    return jsonify([serialize_producto(p) for p in prods])

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
        db.session.add(p)
        db.session.commit()
        return ok_json({"ok": True, "producto": serialize_producto(p)}, 201)
    except Exception as e:
        db.session.rollback()
        return json_error("Error creando producto", 500, str(e))

@app.put("/api/productos/<int:pid>")
def productos_update(pid):
    data = request.get_json(force=True)
    p = Producto.query.get_or_404(pid)
    try:
        if "nombre" in data: p.nombre = str(data["nombre"]).strip()
        if "precio" in data: p.precio = float(data["precio"])
        if "cantidad" in data: p.cantidad = int(float(data["cantidad"]))
        if "porcion_litros" in data: p.porcion_litros = int(data["porcion_litros"])
        if "slot" in data: p.slot_id = int(data["slot"])
        if "habilitado" in data: p.habilitado = bool(data["habilitado"])
        db.session.commit()
        return ok_json({"ok": True, "producto": serialize_producto(p)})
    except Exception as e:
        db.session.rollback()
        return json_error("Error actualizando producto", 500, str(e))

@app.delete("/api/productos/<int:pid>")
def productos_delete(pid):
    p = Producto.query.get_or_404(pid)
    try:
        db.session.delete(p)
        db.session.commit()
        return ok_json({"ok": True}, 204)
    except Exception as e:
        db.session.rollback()
        return json_error("Error eliminando producto", 500, str(e))

@app.post("/api/productos/<int:pid>/reponer")
def productos_reponer(pid):
    p = Producto.query.get_or_404(pid)
    litros = _to_int((request.get_json(force=True) or {}).get("litros", 0))
    p.cantidad = max(0, p.cantidad + litros)
    db.session.commit()
    return ok_json({"ok": True, "producto": serialize_producto(p)})

@app.post("/api/productos/<int:pid>/reset_stock")
def productos_reset(pid):
    p = Producto.query.get_or_404(pid)
    litros = _to_int((request.get_json(force=True) or {}).get("litros", 0))
    p.cantidad = litros
    db.session.commit()
    return ok_json({"ok": True, "producto": serialize_producto(p)})

# -------------------------------------------------------------
# Pagos
# -------------------------------------------------------------
@app.get("/api/pagos")
def pagos_list():
    pagos = Pago.query.order_by(Pago.id.desc()).limit(50).all()
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

@app.get("/api/pagos/pendiente")
def pagos_pendiente():
    p = Pago.query.filter(Pago.estado=="approved", Pago.dispensado==False).order_by(Pago.created_at.asc()).first()
    if not p:
        return jsonify({"ok": True, "pago": None})
    return jsonify({"ok": True, "pago": {
        "id": p.id, "mp_payment_id": p.mp_payment_id, "estado": p.estado,
        "litros": p.litros, "slot_id": p.slot_id, "product_id": p.product_id
    }})

@app.post("/api/pagos/<int:pid>/reenviar")
def pagos_reenviar(pid):
    p = Pago.query.get_or_404(pid)
    if p.estado != "approved" or p.dispensado:
        return jsonify({"ok": False, "msg": "No se puede reenviar"})
    ok = send_dispense_cmd(p.mp_payment_id, p.slot_id, p.litros, timeout_s=max(30, p.litros*5))
    return jsonify({"ok": ok})

# -------------------------------------------------------------
# Mercado Pago
# -------------------------------------------------------------
@app.post("/api/pagos/preferencia")
def crear_preferencia():
    data = request.get_json(force=True, silent=True) or {}
    product_id = _to_int(data.get("product_id") or 0)
    litros_req = _to_int(data.get("litros") or 0)
    prod = Producto.query.get(product_id)
    if not prod or not prod.habilitado:
        return json_error("producto no disponible", 400)
    litros = litros_req if litros_req > 0 else prod.porcion_litros
    backend_base = BACKEND_BASE_URL or request.url_root.rstrip("/")
    body = {
        "items": [{
            "id": str(prod.id),
            "title": prod.nombre,
            "quantity": 1,
            "currency_id": "ARS",
            "unit_price": float(prod.precio),
        }],
        "metadata": {"slot_id": prod.slot_id, "product_id": prod.id, "producto": prod.nombre, "litros": litros},
        "external_reference": f"pid={prod.id};slot={prod.slot_id};litros={litros}",
        "auto_return": "approved",
        "back_urls": {"success": WEB_URL, "failure": WEB_URL, "pending": WEB_URL},
        "notification_url": f"{backend_base}/api/mp/webhook",
        "statement_descriptor": "DISPEN-EASY",
    }
    r = requests.post("https://api.mercadopago.com/checkout/preferences",
                      headers={"Authorization": f"Bearer {MP_ACCESS_TOKEN}", "Content-Type": "application/json"},
                      json=body, timeout=20)
    pref = r.json() or {}
    link = pref.get("init_point") or pref.get("sandbox_init_point")
    return ok_json({"ok": True, "link": link, "raw": pref})

@app.post("/api/mp/webhook")
def mp_webhook():
    return "ok", 200

# -------------------------------------------------------------
# Inicializar MQTT
# -------------------------------------------------------------
with app.app_context():
    try:
        start_mqtt_background()
    except Exception:
        app.logger.exception("[MQTT] error iniciando hilo")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
