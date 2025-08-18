# app.py
import os
import json
from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS

# --- MQTT ---
import ssl
import paho.mqtt.client as mqtt

# --- MercadoPago SDK ---
import mercadopago

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})

# =========================
# Config DB (Railway / Postgres)
# =========================
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

# =========================
# Modelo
# =========================
class Producto(db.Model):
    __tablename__ = "producto"
    id = db.Column(db.Integer, primary_key=True)         # id interno autoincremental
    slot_id = db.Column(db.Integer, nullable=False)       # 1..6 (fijo)
    nombre = db.Column(db.String(120), nullable=False, default="")
    precio = db.Column(db.Float, nullable=False, default=0.0)
    cantidad = db.Column(db.Float, nullable=False, default=0.0)  # litros
    habilitado = db.Column(db.Boolean, nullable=False, default=False)

    def to_dict(self):
        return {
            "id": self.id,
            "slot_id": self.slot_id,
            "nombre": self.nombre,
            "precio": self.precio,
            "cantidad": self.cantidad,
            "habilitado": self.habilitado,
        }

# =========================
# Helpers de inicialización
# =========================
def seed_slots_si_faltan():
    """
    Garantiza que existan exactamente 6 filas (slot_id 1..6).
    Si falta alguna, la crea con valores por defecto.
    """
    existentes = {p.slot_id for p in Producto.query.all()}
    faltantes = [s for s in range(1, 7) if s not in existentes]
    for s in faltantes:
        p = Producto(slot_id=s, nombre=f"Producto {s}", precio=0.0, cantidad=0.0, habilitado=False)
        db.session.add(p)
    if faltantes:
        db.session.commit()

with app.app_context():
    db.create_all()
    seed_slots_si_faltan()

# =========================
# MQTT helpers
# =========================
def _mqtt_client():
    broker   = os.getenv("MQTT_BROKER", "")
    port     = int(os.getenv("MQTT_PORT", "8883"))
    user     = os.getenv("MQTT_USER", "")
    pwd      = os.getenv("MQTT_PASS", "")
    client_id = os.getenv("MQTT_CLIENT_ID", "dispen-easy-backend")

    if not broker:
        raise RuntimeError("Falta MQTT_BROKER")

    client = mqtt.Client(client_id=client_id, clean_session=True)
    if user or pwd:
        client.username_pw_set(user, pwd)

    # TLS por defecto si es 8883
    if port == 8883:
        client.tls_set(tls_version=ssl.PROTOCOL_TLS)
        client.tls_insecure_set(False)

    return client, broker, port

def mqtt_publicar(payload: dict, retain: bool = False, qos: int = 1):
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

# =========================
# MercadoPago SDK
# =========================
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "").strip()
if not MP_ACCESS_TOKEN:
    print("[MP] WARNING: Falta MP_ACCESS_TOKEN")

mp_sdk = mercadopago.SDK(MP_ACCESS_TOKEN) if MP_ACCESS_TOKEN else None

# =========================
# Rutas básicas
# =========================
@app.route("/")
def root():
    return jsonify({"mensaje": "API Dispen-Easy OK"})

# ---------- CRUD de 6 slots ----------
@app.route("/api/productos", methods=["GET"])
def listar_productos():
    rows = Producto.query.order_by(Producto.slot_id.asc()).all()
    # por si en alguna migración faltara, garantizamos 6
    if len(rows) < 6:
        seed_slots_si_faltan()
        rows = Producto.query.order_by(Producto.slot_id.asc()).all()
    return jsonify([r.to_dict() for r in rows])

@app.route("/api/productos/<int:slot_id>", methods=["PUT"])
def actualizar_producto(slot_id):
    if not (1 <= slot_id <= 6):
        return jsonify({"error": "slot_id inválido"}), 400

    data = request.get_json(force=True) or {}
    p = Producto.query.filter_by(slot_id=slot_id).first()
    if not p:
        # si no existe por algún motivo, lo creamos
        p = Producto(slot_id=slot_id)
        db.session.add(p)

    p.nombre = str(data.get("nombre", p.nombre or "")).strip()
    p.precio = float(data.get("precio", p.precio or 0.0))
    p.cantidad = float(data.get("cantidad", p.cantidad or 0.0))
    p.habilitado = bool(data.get("habilitado", p.habilitado))

    db.session.commit()
    return jsonify(p.to_dict())

@app.route("/api/productos/<int:slot_id>", methods=["DELETE"])
def limpiar_producto(slot_id):
    if not (1 <= slot_id <= 6):
        return jsonify({"error": "slot_id inválido"}), 400
    p = Producto.query.filter_by(slot_id=slot_id).first()
    if not p:
        return jsonify({"error": "slot no encontrado"}), 404
    # No borramos la fila; la reseteamos
    p.nombre = f"Producto {slot_id}"
    p.precio = 0.0
    p.cantidad = 0.0
    p.habilitado = False
    db.session.commit()
    return jsonify({"ok": True, "slot_id": slot_id})

# ---------- Generar QR / Preferencia ----------
@app.route("/api/generar_qr/<int:slot_id>", methods=["GET"])
def generar_qr(slot_id):
    if not mp_sdk:
        return jsonify({"error": "MP SDK no inicializado (falta MP_ACCESS_TOKEN)"}), 500

    p = Producto.query.filter_by(slot_id=slot_id).first()
    if not p:
        return jsonify({"error": "Producto no encontrado"}), 404

    if not p.habilitado or not p.nombre or p.precio <= 0:
        return jsonify({"error": "Producto no habilitado o datos incompletos"}), 400

    # Armar preferencia
    preference_data = {
        "items": [{
            "title": p.nombre,
            "quantity": 1,
            "unit_price": float(p.precio)
        }],
        "metadata": {
            "producto_id": p.id,
            "slot_id": p.slot_id
        },
        # opcional: descripción visible
        "description": p.nombre,
        "notification_url": os.getenv("WEBHOOK_URL", "").strip() or request.url_root.rstrip("/") + "/webhook",
        "auto_return": "approved",
        "back_urls": {
            "success": request.url_root,
            "pending": request.url_root,
            "failure": request.url_root
        }
    }

    try:
        pref_resp = mp_sdk.preference().create(preference_data)
        # SDK devuelve dict con 'response' adentro
        pref = pref_resp.get("response", {})
        init_point = pref.get("init_point") or pref.get("sandbox_init_point")
        if not init_point:
            return jsonify({"error": "No se pudo obtener init_point", "detalle": pref}), 502
        return jsonify({"link": init_point, "slot_id": slot_id})
    except Exception as e:
        return jsonify({"error": "MP error creando preferencia", "detalle": str(e)}), 502

# ---------- Webhook MP ----------
@app.route("/webhook", methods=["POST"])
def webhook():
    raw = request.get_json(silent=True) or {}
    print("[webhook] raw:", raw, flush=True)

    token = MP_ACCESS_TOKEN
    if not token or not mp_sdk:
        print("[webhook] Falta MP_ACCESS_TOKEN", flush=True)
        return jsonify({"error": "missing token"}), 500

    # Resolver payment_id (v2: data.id; clásico: resource/topic)
    payment_id = None

    if isinstance(raw.get("data"), dict) and raw["data"].get("id"):
        payment_id = str(raw["data"]["id"])

    if not payment_id and raw.get("resource") and raw.get("topic"):
        resource = str(raw["resource"]).rstrip("/")
        topic = str(raw["topic"])
        if topic == "payment" and "/payments/" in resource:
            payment_id = resource.split("/")[-1]

    if not payment_id:
        print("[webhook] sin payment_id -> ignored", flush=True)
        return jsonify({"status": "ignored"}), 200

    try:
        pay_resp = mp_sdk.payment().get(payment_id)
        data = pay_resp.get("response", {}) if isinstance(pay_resp, dict) else {}
    except Exception as e:
        print("[webhook] error payment.get:", e, flush=True)
        return jsonify({"status": "ok"}), 200

    estado = (data.get("status") or "").lower()
    # Buscar metadata segura
    meta = data.get("metadata") or {}
    producto_id = meta.get("producto_id")
    slot_id = meta.get("slot_id")

    # Fallback por si metadata no vino
    if not slot_id and data.get("additional_info", {}).get("items"):
        try:
            slot_id = int(data["additional_info"]["items"][0].get("id") or 0)
        except Exception:
            slot_id = None

    print(f"[webhook] pago {payment_id} -> {estado} meta:", meta, flush=True)

    # Guardar/actualizar estado en DB rápido (opcional)
    # acá no llevamos una tabla de pagos para simplificar

    # Si aprobado -> publicar a MQTT
    if estado == "approved" and slot_id:
        try:
            payload = {
                "ok": 1,
                "pago_id": str(payment_id),
                "slot_id": int(slot_id),
                "producto_id": producto_id
            }
            mqtt_publicar(payload, retain=False, qos=1)
            print("[webhook] MQTT publicado:", payload, flush=True)
        except Exception as e:
            print("[webhook] Error publicando MQTT:", e, flush=True)

    return jsonify({"status": "ok"}), 200

# =========================
# Main (gunicorn en Railway)
# =========================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
