# app.py
import os, json, io, base64, ssl
from flask import Flask, request, jsonify, abort
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
import requests
import qrcode
import paho.mqtt.client as mqtt

# ----------------------------
# Config básica
# ----------------------------
app = Flask(__name__)

# DB (Railway -> DATABASE_URL)
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

# CORS: permitir front en Railway + localhost
FRONT_ORIGIN = os.getenv("FRONT_ORIGIN", "")
CORS(app, resources={
    r"/api/*": {
        "origins": [FRONT_ORIGIN, "http://localhost:3000", "http://127.0.0.1:3000"],
        "supports_credentials": False
    },
    r"/webhook": {"origins": "*"},
})

# ----------------------------
# Modelo: 6 slots fijos
# ----------------------------
class Producto(db.Model):
    __tablename__ = "producto"
    id         = db.Column(db.Integer, primary_key=True)              # autonumérico (trazabilidad)
    slot_id    = db.Column(db.Integer, unique=True, nullable=False)   # 0..5 mapea a GPIO
    nombre     = db.Column(db.String(120), nullable=False, default="")
    precio     = db.Column(db.Float, nullable=False, default=0.0)
    cantidad   = db.Column(db.Integer, nullable=False, default=0)
    habilitado = db.Column(db.Boolean, nullable=False, default=False)

    def to_dict(self):
        return {
            "slot_id": self.slot_id,
            "nombre": self.nombre,
            "precio": self.precio,
            "cantidad": self.cantidad,
            "habilitado": self.habilitado
        }

# Crear tablas + sembrar 6 slots si faltan
with app.app_context():
    db.create_all()

    existentes = {p.slot_id for p in Producto.query.all()}
    changed = False
    for sid in range(6):
        if sid not in existentes:
            db.session.add(Producto(slot_id=sid))
            changed = True
    if changed:
        db.session.commit()
        print("[DB] Sembrados slots faltantes (0..5)")

# ----------------------------
# Helpers Mercado Pago
# ----------------------------
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "").strip()
BACKEND_BASE    = os.getenv("BACKEND_BASE", "").strip()   # ej: https://web-production-xxxx.up.railway.app
FRONT_BASE      = os.getenv("FRONT_BASE", "").strip()     # opcional (para back_urls)

def mp_create_preference(producto: Producto):
    if not MP_ACCESS_TOKEN:
        raise RuntimeError("Falta MP_ACCESS_TOKEN")

    titulo = producto.nombre or f"Slot {producto.slot_id + 1}"
    payload = {
        "items": [{
            "title": titulo,
            "quantity": 1,
            "unit_price": float(producto.precio),
        }],
        "description": titulo,
        "additional_info": {"items": [{"title": titulo}]},
        "metadata": {
            "slot_id": producto.slot_id,
            "producto_id": producto.id,
            "producto_nombre": producto.nombre,
        },
        "external_reference": f"s{producto.slot_id}",
        "auto_return": "approved",
    }

    # back_urls opcionales
    if FRONT_BASE:
        payload["back_urls"] = {
            "success": FRONT_BASE,
            "pending": FRONT_BASE,
            "failure": FRONT_BASE,
        }

    # webhook/notification_url -> este backend
    if BACKEND_BASE:
        payload["notification_url"] = f"{BACKEND_BASE}/webhook"

    headers = {"Authorization": f"Bearer {MP_ACCESS_TOKEN}"}
    r = requests.post(
        "https://api.mercadopago.com/checkout/preferences",
        headers=headers, json=payload, timeout=12
    )
    if r.status_code != 201:
        raise RuntimeError(f"MP error {r.status_code}: {r.text}")

    return r.json()

def mp_get_payment(payment_id: str):
    headers = {"Authorization": f"Bearer {MP_ACCESS_TOKEN}"}
    r = requests.get(
        f"https://api.mercadopago.com/v1/payments/{payment_id}",
        headers=headers, timeout=12
    )
    if r.status_code != 200:
        raise RuntimeError(f"MP get payment {r.status_code}: {r.text}")
    return r.json()

def mo_get_last_payment_id(merchant_order_id: str):
    headers = {"Authorization": f"Bearer {MP_ACCESS_TOKEN}"}
    r = requests.get(
        f"https://api.mercadopago.com/merchant_orders/{merchant_order_id}",
        headers=headers, timeout=12
    )
    if r.status_code != 200:
        raise RuntimeError(f"MP get MO {r.status_code}: {r.text}")
    data = r.json()
    payments = data.get("payments", []) or []
    if not payments:
        return None
    return str(payments[-1].get("id"))

# ----------------------------
# Helper MQTT (HiveMQ TLS 8883)
# ----------------------------
def mqtt_publish(payload: dict, retain=False, qos=1):
    broker   = os.getenv("MQTT_BROKER")         # c9b4a2...s1.eu.hivemq.cloud
    port     = int(os.getenv("MQTT_PORT", "8883"))
    user     = os.getenv("MQTT_USER")
    pwd      = os.getenv("MQTT_PASS")
    topic    = os.getenv("MQTT_TOPIC", "dispen-easy/dispensar")
    keepaliv = int(os.getenv("MQTT_KEEPALIVE", "60"))

    if not all([broker, user, pwd]):
        raise RuntimeError("Faltan variables MQTT_BROKER/MQTT_USER/MQTT_PASS")

    client = mqtt.Client(client_id="dispen-back", clean_session=True)
    client.username_pw_set(user, pwd)
    client.tls_set(tls_version=ssl.PROTOCOL_TLS, cert_reqs=ssl.CERT_REQUIRED)
    client.tls_insecure_set(False)

    client.connect(broker, port, keepalive=keepaliv)
    client.loop_start()
    try:
        info = client.publish(topic, json.dumps(payload), qos=qos, retain=retain)
        info.wait_for_publish(timeout=5)
    finally:
        client.loop_stop()
        client.disconnect()

# ----------------------------
# Endpoints API
# ----------------------------
@app.route("/")
def root():
    return jsonify({"mensaje": "API de Dispen-Easy funcionando"})

# Listar SIEMPRE 6 slots (0..5) ordenados
@app.route("/api/productos", methods=["GET"])
def listar_productos():
    productos = Producto.query.order_by(Producto.slot_id.asc()).all()
    # asegurador: si faltara alguno por X motivo, completar vacío
    by_slot = {p.slot_id: p for p in productos}
    out = []
    for sid in range(6):
        p = by_slot.get(sid) or Producto(slot_id=sid)
        out.append(p.to_dict())
    return jsonify(out), 200

# Actualizar un slot
@app.route("/api/productos/<int:slot_id>", methods=["PUT"])
def actualizar_producto(slot_id: int):
    p = Producto.query.filter_by(slot_id=slot_id).first()
    if not p:
        # si por algún motivo no existe, crearlo
        p = Producto(slot_id=slot_id)
        db.session.add(p)

    data = request.get_json(force=True) or {}
    if "nombre" in data:     p.nombre = str(data["nombre"])
    if "precio" in data:     p.precio = float(data["precio"])
    if "cantidad" in data:   p.cantidad = int(data["cantidad"])
    if "habilitado" in data: p.habilitado = bool(data["habilitado"])
    db.session.commit()
    return jsonify(p.to_dict()), 200

# Generar QR para un slot
@app.route("/api/generar_qr/<int:slot_id>", methods=["GET"])
def generar_qr(slot_id: int):
    p = Producto.query.filter_by(slot_id=slot_id).first()
    if not p:
        abort(404, description="Slot no encontrado")
    if not p.habilitado or p.precio <= 0:
        abort(400, description="Slot deshabilitado o precio inválido")

    pref = mp_create_preference(p)
    link = pref.get("init_point")

    # PNG Base64 del QR
    img = qrcode.make(link)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    qr_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

    return jsonify({"qr_base64": qr_b64, "link": link}), 200

# Webhook MP -> publica MQTT según slot_id
@app.route("/webhook", methods=["POST"])
def webhook():
    raw = request.get_json(silent=True) or {}
    print("[webhook] raw:", raw, flush=True)

    if not MP_ACCESS_TOKEN:
        return jsonify({"error": "missing token"}), 500

    payment_id = None

    # formato nuevo {"data":{"id":123}}
    if isinstance(raw.get("data"), dict) and raw["data"].get("id"):
        payment_id = str(raw["data"]["id"])

    # formato merchant_order (buscar último payment)
    if not payment_id and raw.get("topic") == "merchant_order" and raw.get("resource"):
        mo_id = str(raw["resource"]).rstrip("/").split("/")[-1]
        try:
            payment_id = mo_get_last_payment_id(mo_id)
        except Exception as e:
            print("[webhook] error MO:", e, flush=True)

    if not payment_id:
        print("[webhook] sin payment_id -> ignored", flush=True)
        return jsonify({"status": "ignored"}), 200

    # traer detalle de pago
    try:
        pay = mp_get_payment(payment_id)
        print("[webhook] payment:", {"id": pay.get("id"), "status": pay.get("status")}, flush=True)
    except Exception as e:
        print("[webhook] error get payment:", e, flush=True)
        return jsonify({"status": "ok"}), 200

    estado = (pay.get("status") or "").lower()
    meta   = pay.get("metadata") or {}
    slot_id = meta.get("slot_id")

    if estado == "approved" and isinstance(slot_id, int):
        try:
            mqtt_publish({
                "comando": "activar",
                "slot_id": slot_id,
                "pago_id": str(payment_id),
            })
            print("[webhook] MQTT publicado OK", flush=True)
        except Exception as e:
            print("[webhook] Error publicando MQTT:", e, flush=True)

    return jsonify({"status": "ok"}), 200


# Run local (no se usa en Railway)
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
