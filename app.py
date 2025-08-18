# app.py
import os
import json
import ssl
from datetime import datetime

from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS

import mercadopago
import paho.mqtt.client as mqtt

# -------------------------
# Flask & CORS
# -------------------------
app = Flask(__name__)

FRONT_ORIGIN = os.getenv("FRONT_ORIGIN", "*")
CORS(app, resources={r"/api/*": {"origins": [FRONT_ORIGIN]}},
     supports_credentials=False)

# -------------------------
# DB config
# -------------------------
db_url = os.getenv("DATABASE_URL", "")
# railway a veces entrega postgres:// -> SQLAlchemy requiere postgresql+psycopg2://
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql+psycopg2://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

# -------------------------
# Modelos
# -------------------------
class Producto(db.Model):
    __tablename__ = "producto"
    id = db.Column(db.Integer, primary_key=True)
    slot_id = db.Column(db.Integer, nullable=False, unique=True)   # 1..6
    nombre = db.Column(db.String(120), nullable=False, default="")
    precio = db.Column(db.Float, nullable=False, default=0.0)
    cantidad = db.Column(db.Integer, nullable=False, default=0)     # litros (int)
    habilitado = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "slot_id": self.slot_id,
            "nombre": self.nombre,
            "precio": self.precio,
            "cantidad": self.cantidad,
            "habilitado": self.habilitado,
        }

# -------------------------
# Inicialización DB (6 filas)
# -------------------------
with app.app_context():
    db.create_all()
    # asegurar 6 slots (1..6)
    existentes = {p.slot_id for p in Producto.query.all()}
    for s in range(1, 7):
        if s not in existentes:
            db.session.add(Producto(slot_id=s, nombre=f"Producto {s}",
                                    precio=0.0, cantidad=0, habilitado=False))
    db.session.commit()

# -------------------------
# Helpers
# -------------------------
def mqtt_publish(payload: dict, retain: bool = False) -> bool:
    """Publica un JSON en el tópico configurado. Conecta, envía y cierra."""
    try:
        broker = os.getenv("MQTT_BROKER")
        port = int(os.getenv("MQTT_PORT", "8883"))
        user = os.getenv("MQTT_USER")
        pwd = os.getenv("MQTT_PASS")
        topic = os.getenv("MQTT_TOPIC", "dispen-easy/dispensar")

        if not all([broker, user, pwd]):
            print("[MQTT] Faltan variables (broker/user/pass)")
            return False

        client = mqtt.Client(clean_session=True)
        client.username_pw_set(user, pwd)

        # TLS seguro por defecto
        context = ssl.create_default_context()
        client.tls_set_context(context)
        client.tls_insecure_set(False)

        client.connect(broker, port, keepalive=int(os.getenv("MQTT_KEEPALIVE", "60")))
        client.loop_start()
        info = client.publish(topic, json.dumps(payload), qos=1, retain=retain)
        info.wait_for_publish(timeout=5)
        client.loop_stop()
        client.disconnect()
        print(f"[MQTT] publicado -> {topic} {payload}")
        return True
    except Exception as e:
        print("[MQTT] error:", e)
        return False


def mp_sdk():
    token = os.getenv("MP_ACCESS_TOKEN", "")
    if not token:
        raise RuntimeError("Falta MP_ACCESS_TOKEN")
    return mercadopago.SDK(token)

# -------------------------
# Rutas base
# -------------------------
@app.route("/")
def root():
    return jsonify({"mensaje": "API de Dispen-Easy funcionando"})

# -------------------------
# CRUD de productos (6 slots fijos)
# -------------------------
@app.route("/api/productos", methods=["GET"])
def listar_productos():
    # Siempre devolver 1..6 en orden
    productos = {p.slot_id: p for p in Producto.query.order_by(Producto.slot_id.asc()).all()}
    filas = []
    for s in range(1, 7):
        p = productos.get(s)
        filas.append(p.to_dict() if p else {
            "id": None, "slot_id": s, "nombre": f"Producto {s}",
            "precio": 0.0, "cantidad": 0, "habilitado": False
        })
    return jsonify(filas)

@app.route("/api/productos/<int:slot_id>", methods=["PUT"])
def actualizar_producto(slot_id):
    if slot_id < 1 or slot_id > 6:
        return jsonify({"error": "slot inválido"}), 400

    data = request.get_json(force=True) or {}
    p = Producto.query.filter_by(slot_id=slot_id).first()
    if not p:
        p = Producto(slot_id=slot_id)
        db.session.add(p)

    p.nombre = str(data.get("nombre", p.nombre or ""))
    p.precio = float(data.get("precio", p.precio or 0.0) or 0)
    p.cantidad = int(data.get("cantidad", p.cantidad or 0) or 0)
    p.habilitado = bool(data.get("habilitado", p.habilitado))

    db.session.commit()
    return jsonify(p.to_dict())

@app.route("/api/productos/<int:slot_id>", methods=["DELETE"])
def resetear_producto(slot_id):
    if slot_id < 1 or slot_id > 6:
        return jsonify({"error": "slot inválido"}), 400
    p = Producto.query.filter_by(slot_id=slot_id).first()
    if p:
        p.nombre = f"Producto {slot_id}"
        p.precio = 0.0
        p.cantidad = 0
        p.habilitado = False
        db.session.commit()
    return jsonify({"ok": True, "slot_id": slot_id})

# -------------------------
# Generar QR (MercadoPago)
# -------------------------
@app.route("/api/generar_qr/<int:slot_id>", methods=["GET"])
def generar_qr(slot_id):
    p = Producto.query.filter_by(slot_id=slot_id).first()
    if not p or not p.habilitado or p.precio <= 0 or not p.nombre:
        return jsonify({"error": "Producto inválido o no habilitado"}), 400

    try:
        sdk = mp_sdk()
        # Notificación al webhook de este mismo backend
        base = request.url_root.strip("/")  # https://web-production-xxxx.up.railway.app
        preference_data = {
            "items": [{
                "title": p.nombre,
                "quantity": 1,
                "currency_id": "ARS",
                "unit_price": float(p.precio),
            }],
            "metadata": {
                "slot_id": p.slot_id,
                "producto_id": p.id,
            },
            "back_urls": {
                "success": base + "/",
                "failure": base + "/",
                "pending": base + "/",
            },
            "auto_return": "approved",
            "notification_url": base + "/webhook",
            "external_reference": f"prod:{p.id}"
        }

        resp = sdk.preference().create(preference_data)
        pref = resp["response"]
        # init_point (web) o point_of_interaction.transaction_data.qr_code_base64 (si usás QR omnicanal)
        return jsonify({
            "init_point": pref.get("init_point"),
            "id": pref.get("id")
        })
    except Exception as e:
        print("[MP] error creando preferencia:", e)
        return jsonify({"error": "No se pudo generar la preferencia"}), 500

# -------------------------
# Webhook MercadoPago
# -------------------------
@app.route("/webhook", methods=["POST"])
def webhook():
    raw = request.get_json(silent=True) or {}
    print("[webhook] raw:", raw, flush=True)

    token = os.getenv("MP_ACCESS_TOKEN")
    if not token:
        print("[webhook] falta MP_ACCESS_TOKEN", flush=True)
        return jsonify({"status": "missing token"}), 500

    # 1) Resolver payment_id
    payment_id = None

    # formato nuevo (data.id)
    if isinstance(raw.get("data"), dict) and raw["data"].get("id"):
        payment_id = str(raw["data"]["id"])

    # clásico (resource/topic)
    if not payment_id and raw.get("resource") and raw.get("topic"):
        res = str(raw["resource"]).rstrip("/")
        if "/payments/" in res:
            payment_id = res.split("/")[-1]
        elif raw.get("topic") == "merchant_order" and "/merchant_orders/" in res:
            # buscar último payment de la orden
            try:
                import requests
                mo_id = res.split("/")[-1]
                r = requests.get(
                    f"https://api.mercadolibre.com/merchant_orders/{mo_id}",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=12,
                )
                jo = r.json()
                pays = jo.get("payments") or []
                if pays:
                    payment_id = str(pays[-1].get("id"))
            except Exception as e:
                print("[webhook] error merchant_orders:", e, flush=True)

    if not payment_id:
        print("[webhook] sin payment_id -> ignored", flush=True)
        return jsonify({"status": "ignored"}), 200

    # 2) Traer detalle del pago
    try:
        sdk = mp_sdk()
        pay_resp = sdk.payment().get(payment_id)
        pay = pay_resp["response"]
        print("[webhook] payment:", {"id": pay.get("id"), "status": pay.get("status")}, flush=True)
    except Exception as e:
        print("[webhook] error consultando payment:", e, flush=True)
        return jsonify({"status": "ok"}), 200

    estado = (pay.get("status") or "").lower()
    meta = pay.get("metadata") or {}
    slot_id = meta.get("slot_id")

    if estado == "approved" and slot_id:
        # Publicar MQTT: el ESP32 debe accionar el relé del slot indicado
        payload = {"accion": "dispensar", "slot_id": int(slot_id)}
        ok = mqtt_publish(payload, retain=False)
        print("[webhook] MQTT:", ok, payload, flush=True)

    return jsonify({"status": "ok"}), 200


# -------------------------
# Entrypoint local
# -------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
