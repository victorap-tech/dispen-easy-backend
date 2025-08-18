# app.py  — Dispen-Easy (PostgreSQL + MP + MQTT)

import os, io, base64, json, ssl
from datetime import datetime

from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import UniqueConstraint
import requests
import qrcode
import paho.mqtt.client as mqtt

# -----------------------------
# Config Flask + CORS
# -----------------------------
app = Flask(__name__)

allowed = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()]
if allowed:
    CORS(app, resources={r"/api/*": {"origins": allowed}})
else:
    CORS(app)

# -----------------------------
# DB (Railway Postgres)
# -----------------------------
db_url = os.getenv("DATABASE_URL", "")
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)
if not db_url:
    raise RuntimeError("Falta DATABASE_URL")

app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

TOTAL_SLOTS = 6

# -----------------------------
# Modelo
# -----------------------------
class Producto(db.Model):
    __tablename__ = "producto"

    id         = db.Column(db.Integer, primary_key=True)
    slot_id    = db.Column(db.Integer, nullable=False)         # 1..6
    nombre     = db.Column(db.String(120), nullable=False, default="")
    precio     = db.Column(db.Float, nullable=False, default=0.0)
    cantidad   = db.Column(db.Integer, nullable=False, default=1)
    habilitado = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (UniqueConstraint("slot_id", name="uq_producto_slot"),)

    def to_dict(self):
        return {
            "id": self.id,
            "slot_id": self.slot_id,
            "nombre": self.nombre,
            "precio": self.precio,
            "cantidad": self.cantidad,
            "habilitado": self.habilitado,
        }

with app.app_context():
    db.create_all()

# -----------------------------
# Helpers varios
# -----------------------------
def fila_vacia(slot_id: int):
    return {
        "id": None, "slot_id": slot_id, "nombre": "",
        "precio": 0.0, "cantidad": 1, "habilitado": False
    }

def _mqtt_client():
    broker   = os.getenv("MQTT_BROKER")
    port     = int(os.getenv("MQTT_PORT", "8883"))
    user     = os.getenv("MQTT_USER")
    pwd      = os.getenv("MQTT_PASS")
    client_id= os.getenv("MQTT_CLIENT_ID", "dispen-easy-backend")

    if not all([broker, user, pwd]):
        raise RuntimeError("Faltan variables MQTT_BROKER / MQTT_USER / MQTT_PASS")

    c = mqtt.Client(client_id=client_id, clean_session=True)
    c.username_pw_set(user, pwd)
    # TLS seguro
    c.tls_set(tls_version=ssl.PROTOCOL_TLS)
    c.tls_insecure_set(False)
    return c, broker, port

def mqtt_publish(payload: dict, retain: bool=False, qos: int=1) -> None:
    topic = os.getenv("MQTT_TOPIC", "dispen-easy/dispensar")
    client, broker, port = _mqtt_client()
    client.connect(broker, port, keepalive=int(os.getenv("MQTT_KEEPALIVE", "60")))
    client.loop_start()
    try:
        info = client.publish(topic, json.dumps(payload), qos=qos, retain=retain)
        info.wait_for_publish(timeout=5)
    finally:
        client.loop_stop()
        client.disconnect()

def mp_get_payment(payment_id: str, token: str):
    url = f"https://api.mercadopago.com/v1/payments/{payment_id}"
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.get(url, headers=headers, timeout=12)
    raw = r.json()
    if r.status_code != 200:
        return None, r.status_code, raw
    # nombre del producto por si querés loguear
    producto_nombre = (
        raw.get("description")
        or (raw.get("additional_info", {}).get("items", [{}])[0].get("title"))
        or (raw.get("metadata", {}).get("producto_nombre"))
        or "Producto"
    )
    info = {
        "id_pago": str(raw.get("id")),
        "estado":  raw.get("status"),
        "producto": producto_nombre,
        "slot_id":  raw.get("metadata", {}).get("slot_id"),
        "producto_id": raw.get("metadata", {}).get("producto_id"),
    }
    return info, 200, raw

# -----------------------------
# Rutas básicas / CRUD 6 slots
# -----------------------------
@app.route("/", methods=["GET"])
def root():
    return jsonify({"mensaje": "API Dispen-Easy funcionando"}), 200

@app.route("/api/productos", methods=["GET"])
def listar_productos():
    productos = Producto.query.all()
    por_slot = {p.slot_id: p.to_dict() for p in productos}
    data = [por_slot.get(s, fila_vacia(s)) for s in range(1, TOTAL_SLOTS+1)]
    return jsonify(data), 200

@app.route("/api/productos/<int:slot_id>", methods=["GET"])
def obtener_producto(slot_id):
    p = Producto.query.filter_by(slot_id=slot_id).first()
    return jsonify(p.to_dict() if p else fila_vacia(slot_id)), 200

@app.route("/api/productos/<int:slot_id>", methods=["PUT","POST"])
def upsert_producto(slot_id):
    if slot_id < 1 or slot_id > TOTAL_SLOTS:
        return jsonify({"error": "slot_id fuera de rango (1..6)"}), 400
    data = request.get_json(force=True, silent=True) or {}
    nombre     = (data.get("nombre") or "").strip()
    precio     = float(data.get("precio") or 0)
    cantidad   = int(data.get("cantidad") or 1)
    habilitado = bool(data.get("habilitado", True))
    if not nombre:
        return jsonify({"error": "nombre requerido"}), 400

    p = Producto.query.filter_by(slot_id=slot_id).first()
    if p:
        p.nombre, p.precio, p.cantidad, p.habilitado = nombre, precio, cantidad, habilitado
        db.session.commit()
        return jsonify({"mensaje":"actualizado", "producto": p.to_dict()}), 200
    p = Producto(slot_id=slot_id, nombre=nombre, precio=precio, cantidad=cantidad, habilitado=habilitado)
    db.session.add(p); db.session.commit()
    return jsonify({"mensaje":"creado", "producto": p.to_dict()}), 201

@app.route("/api/productos/<int:slot_id>", methods=["DELETE"])
def delete_producto(slot_id):
    p = Producto.query.filter_by(slot_id=slot_id).first()
    if not p:
        return jsonify({"mensaje": "Nada para borrar"}), 200
    db.session.delete(p); db.session.commit()
    return jsonify({"mensaje":"eliminado", "slot_id": slot_id}), 200

# -----------------------------
# Generar QR (Mercado Pago)
# -----------------------------
@app.route("/api/generar_qr/<int:slot_id>", methods=["GET"])
def generar_qr(slot_id):
    token = os.getenv("MP_ACCESS_TOKEN")
    if not token:
        return jsonify({"error":"Falta MP_ACCESS_TOKEN"}), 500

    p = Producto.query.filter_by(slot_id=slot_id).first()
    if not p or not p.habilitado:
        return jsonify({"error": "Producto no disponible"}), 404

    titulo = p.nombre
    payload = {
        "items": [{
            "title": titulo,
            "quantity": 1,
            "unit_price": float(p.precio),
        }],
        "description": p.nombre,
        "additional_info": {"items": [{"title": p.nombre}]},
        "metadata": {
            "producto_id": p.id,
            "slot_id": p.slot_id,
            "producto_nombre": p.nombre,
        },
        "external_reference": f"prod:{p.id}",
        "notification_url": f"https://{request.host}/webhook",
        "back_urls": {
            "success": f"https://{request.host}/",
            "pending": f"https://{request.host}/",
            "failure": f"https://{request.host}/",
        },
        "auto_return": "approved",
    }

    url = "https://api.mercadopago.com/checkout/preferences"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    r = requests.post(url, headers=headers, json=payload, timeout=12)
    if r.status_code != 201:
        print("MP error:", r.status_code, r.text, flush=True)
        return jsonify({"error":"No se pudo crear preferencia"}), 502

    init_point = r.json().get("init_point")
    # QR PNG -> base64
    buf = io.BytesIO()
    qrcode.make(init_point).save(buf, format="PNG")
    qr_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

    return jsonify({"qr_base64": qr_b64, "link": init_point}), 200

# -----------------------------
# Webhook Mercado Pago -> MQTT
# -----------------------------
@app.route("/webhook", methods=["POST"])
def webhook():
    raw = request.get_json(silent=True) or {}
    print("[webhook] raw:", raw, flush=True)

    token = os.getenv("MP_ACCESS_TOKEN")
    if not token:
        print("[webhook] Falta MP_ACCESS_TOKEN", flush=True)
        return jsonify({"error": "missing token"}), 500

    # Resolver payment_id (v2 / clásico)
    payment_id = None
    if isinstance(raw.get("data"), dict) and raw["data"].get("id"):
        payment_id = str(raw["data"]["id"])
    if not payment_id and raw.get("resource") and raw.get("topic"):
        resource = str(raw["resource"]).rstrip("/")
        topic    = raw["topic"]
        if topic == "payment" and "/payments/" in resource:
            payment_id = resource.split("/")[-1]
        elif topic == "merchant_order" and "/merchant_orders/" in resource:
            mo_id = resource.split("/")[-1]
            try:
                r_mo = requests.get(
                    f"https://api.mercadopago.com/merchant_orders/{mo_id}",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=12
                )
                mo = r_mo.json()
                payments = mo.get("payments") or []
                if payments:
                    payment_id = str(payments[-1].get("id"))
            except Exception as e:
                print("[webhook] Error consultando merchant_orders:", e, flush=True)

    if not payment_id:
        print("[webhook] sin payment_id -> ignored", flush=True)
        return jsonify({"status":"ignored"}), 200

    # Traer detalle y decidir
    try:
        info, st, _raw = mp_get_payment(payment_id, token)
    except Exception as e:
        print("[webhook] error consultando payment:", e, flush=True)
        return jsonify({"status":"ok"}), 200

    if not info:
        print("[webhook] payment no encontrado:", st, flush=True)
        return jsonify({"status":"ok"}), 200

    estado = (info.get("estado") or "").lower()
    slot_id = info.get("slot_id")

    print(f"[webhook] payment resp: {estado} id:{payment_id} slot:{slot_id}", flush=True)

    # Sólo aprobados -> publicar al dispositivo
    if estado == "approved" and slot_id:
        try:
            mqtt_publish({
                "comando": "activar",
                "slot_id": int(slot_id),
                "pago_id": str(payment_id),
            })
            print("[webhook] MQTT publicado OK", flush=True)
        except Exception as e:
            print("[webhook] Error publicando MQTT:", e, flush=True)

    return jsonify({"status":"ok"}), 200


# -----------------------------
# Local
# -----------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
