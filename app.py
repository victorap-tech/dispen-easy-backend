import os, time, json as _json, threading, requests
from threading import Lock, Timer
from collections import defaultdict
from flask import Flask, jsonify, request, Response
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
import paho.mqtt.client as mqtt
from sqlalchemy import and_

# =========================================
# 🔧 CONFIGURACIÓN GENERAL
# =========================================
app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "sqlite:///dispen.db").replace("postgres://", "postgresql://")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)
CORS(app, resources={r"/api/*": {"origins": "*"}})

ADMIN_SECRET = os.getenv("ADMIN_SECRET", "adm123")
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "")
MQTT_HOST = os.getenv("MQTT_HOST", "")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USER = os.getenv("MQTT_USER", "")
MQTT_PASS = os.getenv("MQTT_PASS", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# =========================================
# 🧱 MODELOS
# =========================================
class Dispenser(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    device_id = db.Column(db.String(80), unique=True)
    nombre = db.Column(db.String(120))
    activo = db.Column(db.Boolean, default=True)

class Producto(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    dispenser_id = db.Column(db.Integer, db.ForeignKey("dispenser.id"))
    nombre = db.Column(db.String(100))
    precio = db.Column(db.Float)
    cantidad = db.Column(db.Float)
    slot = db.Column(db.Integer)
    porcion_litros = db.Column(db.Float, default=1)
    habilitado = db.Column(db.Boolean, default=False)
    bundle_precios = db.Column(db.JSON, default={})

class Pago(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    mp_payment_id = db.Column(db.String(100))
    estado = db.Column(db.String(40))
    monto = db.Column(db.Float)
    litros = db.Column(db.Float)
    dispenser_id = db.Column(db.Integer)
    slot_id = db.Column(db.Integer)
    product_id = db.Column(db.Integer)
    device_id = db.Column(db.String(80))
    dispensado = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=db.func.now())

class OperatorToken(db.Model):
    token = db.Column(db.String(100), primary_key=True)
    dispenser_id = db.Column(db.Integer)
    nombre = db.Column(db.String(100))
    activo = db.Column(db.Boolean, default=True)
    chat_id = db.Column(db.String(50), default="")
    created_at = db.Column(db.DateTime, default=db.func.now())

db.create_all()

# =========================================
# 💬 TELEGRAM
# =========================================
def _tg_send_async(text, chat_id=None):
    if not TELEGRAM_BOT_TOKEN:
        return
    dest = chat_id or TELEGRAM_CHAT_ID
    if not dest:
        return
    def _run():
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": dest, "text": text},
                timeout=10
            )
        except Exception as e:
            print(f"[TG ERROR] {e}")
    threading.Thread(target=_run, daemon=True).start()

def notify_all(text, dispenser_id=None):
    """Envía a admin y operadores activos del dispenser."""
    _tg_send_async(text, TELEGRAM_CHAT_ID)
    if dispenser_id:
        toks = OperatorToken.query.filter(
            and_(OperatorToken.dispenser_id == dispenser_id, OperatorToken.activo == True)
        ).all()
        for t in toks:
            cid = (t.chat_id or "").strip()
            if cid:
                _tg_send_async(text, cid)

# =========================================
# 🌐 MERCADO PAGO - Webhook & pagos
# =========================================
@app.route("/api/webhook", methods=["POST"])
def mp_webhook():
    data = request.get_json(force=True)
    mp_id = data.get("data", {}).get("id") or data.get("id")
    if not mp_id:
        return jsonify({"error": "sin id"}), 400
    pago = Pago.query.filter_by(mp_payment_id=str(mp_id)).first()
    if not pago:
        pago = Pago(mp_payment_id=str(mp_id), estado="pending")
        db.session.add(pago)
    pago.estado = data.get("action") or "approved"
    db.session.commit()
    return jsonify({"ok": True})

@app.route("/api/pagos", methods=["GET"])
def get_pagos():
    pagos = Pago.query.order_by(Pago.created_at.desc()).limit(10).all()
    return jsonify([
        {
            "id": p.id, "mp_payment_id": p.mp_payment_id, "estado": p.estado,
            "monto": p.monto, "litros": p.litros, "slot_id": p.slot_id,
            "product_id": p.product_id, "device_id": p.device_id,
            "dispensado": p.dispensado, "created_at": p.created_at
        } for p in pagos
    ])

# =========================================
# 💧 PRODUCTOS
# =========================================
@app.route("/api/productos", methods=["GET"])
def productos_list():
    disp_id = request.args.get("dispenser_id", type=int)
    q = Producto.query
    if disp_id:
        q = q.filter(Producto.dispenser_id == disp_id)
    productos = q.all()
    return jsonify([
        {
            "id": p.id, "nombre": p.nombre, "precio": p.precio,
            "cantidad": p.cantidad, "slot": p.slot,
            "porcion_litros": p.porcion_litros, "habilitado": p.habilitado,
            "bundle_precios": p.bundle_precios
        } for p in productos
    ])

@app.route("/api/productos", methods=["POST"])
def productos_create():
    data = request.get_json(force=True)
    p = Producto(
        dispenser_id=data.get("dispenser_id"),
        nombre=data.get("nombre"),
        precio=data.get("precio"),
        cantidad=data.get("cantidad", 0),
        slot=data.get("slot"),
        porcion_litros=data.get("porcion_litros", 1),
        habilitado=data.get("habilitado", False),
        bundle_precios=data.get("bundle_precios", {})
    )
    db.session.add(p)
    db.session.commit()
    return jsonify({"producto": _json.loads(_json.dumps(p, default=lambda o: o.__dict__))})

@app.route("/api/productos/<int:id>", methods=["PUT"])
def productos_update(id):
    p = Producto.query.get(id)
    if not p:
        return jsonify({"error": "no encontrado"}), 404
    data = request.get_json(force=True)
    for k, v in data.items():
        if hasattr(p, k):
            setattr(p, k, v)
    db.session.commit()
    return jsonify({"producto": {
        "id": p.id, "nombre": p.nombre, "precio": p.precio,
        "cantidad": p.cantidad, "slot": p.slot,
        "porcion_litros": p.porcion_litros, "habilitado": p.habilitado,
        "bundle_precios": p.bundle_precios
    }})

@app.route("/api/productos/<int:id>/reset_stock", methods=["POST"])
def reset_stock(id):
    p = Producto.query.get(id)
    if not p:
        return jsonify({"error": "no encontrado"}), 404
    cantidad = request.json.get("cantidad", 0)
    p.cantidad = cantidad
    db.session.commit()
    notify_all(f"🔄 Stock reseteado {p.nombre}: {cantidad} L", dispenser_id=p.dispenser_id)
    return jsonify({"ok": True})

# =========================================
# ⚙️ DISPENSERS
# =========================================
@app.route("/api/dispensers", methods=["GET"])
def dispensers_list():
    ds = Dispenser.query.all()
    return jsonify([{"id": d.id, "device_id": d.device_id, "nombre": d.nombre, "activo": d.activo} for d in ds])

@app.route("/api/dispensers/<int:id>", methods=["PUT"])
def dispenser_toggle(id):
    d = Dispenser.query.get(id)
    if not d:
        return jsonify({"error": "no encontrado"}), 404
    data = request.get_json(force=True)
    d.activo = data.get("activo", d.activo)
    db.session.commit()
    return jsonify({"dispenser": {"id": d.id, "activo": d.activo}})

# =========================================
# 🧍‍♂️ OPERADORES (TOKENS)
# =========================================
@app.route("/api/admin/operator_tokens", methods=["GET"])
def get_tokens():
    toks = OperatorToken.query.order_by(OperatorToken.created_at.desc()).all()
    return jsonify([
        {
            "token": t.token, "dispenser_id": t.dispenser_id,
            "nombre": t.nombre, "activo": t.activo,
            "chat_id": t.chat_id, "created_at": t.created_at
        } for t in toks
    ])

@app.route("/api/admin/operator_tokens", methods=["POST"])
def crear_token():
    data = request.get_json(force=True)
    import secrets
    tok = secrets.token_hex(8)
    t = OperatorToken(
        token=tok,
        dispenser_id=data.get("dispenser_id"),
        nombre=data.get("nombre"),
        activo=True
    )
    db.session.add(t)
    db.session.commit()
    notify_all(f"🆕 Nuevo operador creado: {t.nombre or tok[:6]}")
    return jsonify({"token": tok})

@app.route("/api/admin/operator_tokens/<string:token>", methods=["PUT"])
def modificar_token(token):
    t = OperatorToken.query.get(token)
    if not t:
        return jsonify({"error": "no encontrado"}), 404
    data = request.get_json(force=True)
    if "activo" in data:
        t.activo = bool(data["activo"])
    if "chat_id" in data:
        t.chat_id = str(data["chat_id"]).strip()
    db.session.commit()
    return jsonify({"ok": True})

@app.route("/api/admin/operator_tokens/<string:token>", methods=["DELETE"])
def borrar_token(token):
    t = OperatorToken.query.get(token)
    if not t:
        return jsonify({"error": "no encontrado"}), 404
    db.session.delete(t)
    db.session.commit()
    return jsonify({"ok": True})

@app.route("/api/operator/link", methods=["POST"])
def operator_link():
    data = request.get_json(force=True)
    token = (data.get("token") or "").strip()
    chat_id = (data.get("chat_id") or "").strip()
    if not token or not chat_id:
        return jsonify({"error": "token y chat_id requeridos"}), 400
    t = OperatorToken.query.get(token)
    if not t:
        return jsonify({"error": "token inválido"}), 404
    t.chat_id = chat_id
    db.session.commit()
    notify_all(f"🔗 Operador vinculado: {t.nombre or token[:6]}", dispenser_id=t.dispenser_id)
    return jsonify({"ok": True})

# =========================================
# 📡 MQTT / STATUS ONLINE-OFFLINE
# =========================================
last_status = defaultdict(lambda: {"status": "unknown", "t": 0})
_last_sent_ts = defaultdict(lambda: {"online": 0.0, "offline": 0.0})
_last_notified_status = defaultdict(lambda: "")
_debounce_timers = defaultdict(lambda: None)
ON_DEBOUNCE_S = 5
COOLDOWN_S = 30 * 60

def _device_notify(dev, status):
    disp = Dispenser.query.filter(Dispenser.device_id == dev).first()
    disp_id = disp.id if disp else None
    icon = "✅" if status == "online" else "⚠️"
    notify_all(f"{icon} {dev}: {status.upper()}", dispenser_id=disp_id)

def _mqtt_on_connect(client, userdata, flags, rc, props=None):
    print(f"[MQTT] Conectado rc={rc}")
    client.subscribe("dispen/+/status")

def _mqtt_on_message(client, userdata, msg):
    raw = msg.payload.decode("utf-8", "ignore")
    dev = msg.topic.split("/")[1]
    st = raw.strip().lower()
    now = time.time()
    last_status[dev] = {"status": st, "t": now}

    if st == "offline":
        _last_sent_ts[dev]["offline"] = now
        _last_notified_status[dev] = "offline"
        with app.app_context():
            _device_notify(dev, "offline")
        t = _debounce_timers.get(dev)
        if t:
            try: t.cancel()
            except: pass
        _debounce_timers[dev] = None
        return

    if st == "online":
        old = _debounce_timers.get(dev)
        if old:
            try: old.cancel()
            except: pass

        def send_if_still_online():
            cur = last_status.get(dev, {"status": "unknown"})
            if cur["status"] == "online":
                now2 = time.time()
                last_sent2 = _last_sent_ts[dev]["online"]
                if not (now2 - last_sent2 < COOLDOWN_S and _last_notified_status[dev] == "online"):
                    _last_sent_ts[dev]["online"] = now2
                    _last_notified_status[dev] = "online"
                    with app.app_context():
                        _device_notify(dev, "online")
            _debounce_timers[dev] = None

        t = Timer(ON_DEBOUNCE_S, send_if_still_online)
        t.daemon = True
        _debounce_timers[dev] = t
        t.start()

def start_mqtt():
    if not MQTT_HOST:
        print("[MQTT] sin host configurado")
        return
    def _run():
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        if MQTT_USER or MQTT_PASS:
            client.username_pw_set(MQTT_USER, MQTT_PASS)
        client.on_connect = _mqtt_on_connect
        client.on_message = _mqtt_on_message
        client.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
        client.loop_forever()
    threading.Thread(target=_run, daemon=True).start()

@app.route("/api/dispensers/status")
def api_disp_status():
    return jsonify([{"device_id": d, "status": v["status"]} for d, v in last_status.items()])

# =========================================
# 🚀 INICIO
# =========================================
if __name__ == "__main__":
    start_mqtt()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
