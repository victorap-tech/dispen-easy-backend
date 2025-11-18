import os
import json
import datetime
import threading

from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS

import requests
import paho.mqtt.client as mqtt

# ============================================================
#   CONFIG FLASK + DB
# ============================================================

app = Flask(__name__)

# CORS para que el frontend en Railway pueda pegarle sin drama
CORS(app, resources={r"/api/*": {"origins": "*"}})

# DB: usamos SQLite por simplicidad (como venías usando)
db_url = os.getenv("DATABASE_URL")
if not db_url:
  db_url = "sqlite:///data.db"
app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

# ============================================================
#   MODELOS
# ============================================================

class Dispenser(db.Model):
    __tablename__ = "dispensers"

    id = db.Column(db.Integer, primary_key=True)
    device_id = db.Column(db.String(64), unique=True, nullable=False)
    nombre = db.Column(db.String(120), nullable=True)
    ubicacion = db.Column(db.String(120), nullable=True)
    tiempo = db.Column(db.Integer, default=23)  # segundos
    precio_frio = db.Column(db.Float, default=100.0)
    precio_caliente = db.Column(db.Float, default=150.0)
    online = db.Column(db.Boolean, default=False)


class Payment(db.Model):
    __tablename__ = "payments"

    id = db.Column(db.Integer, primary_key=True)
    mp_id = db.Column(db.String(64), index=True)
    dispenser_id = db.Column(db.Integer, db.ForeignKey("dispensers.id"))
    tipo = db.Column(db.String(16))  # "fria" / "caliente"
    amount = db.Column(db.Float)
    status = db.Column(db.String(32))
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)


with app.app_context():
    db.create_all()

# ============================================================
#   CONFIG GLOBAL
# ============================================================

ADMIN_SECRET = os.getenv("ADMIN_SECRET", "changeme")

MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "")

MQTT_HOST = os.getenv("MQTT_HOST", "")
MQTT_PORT = int(os.getenv("MQTT_PORT", "8883"))
MQTT_USER = os.getenv("MQTT_USER", "")
MQTT_PASS = os.getenv("MQTT_PASS", "")
TOPIC_PREFIX = os.getenv("TOPIC_PREFIX", "dispen/")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ============================================================
#   HELPERS
# ============================================================

def require_admin():
    hdr = request.headers.get("x-admin-secret", "")
    if hdr != ADMIN_SECRET:
        return False
    return True

def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text})
    except Exception as e:
        print("Error enviando Telegram:", e)

# ============================================================
#   MQTT CLIENT (STATUS + COMMANDS)
# ============================================================

mqtt_client = mqtt.Client()

if MQTT_USER:
    mqtt_client.username_pw_set(MQTT_USER, MQTT_PASS)

# TLS (HiveMQ Cloud)
mqtt_client.tls_set()

def on_mqtt_connect(client, userdata, flags, rc):
    print("MQTT conectado rc=", rc)
    if rc == 0:
        topic = f"{TOPIC_PREFIX}+\/status".replace("\\", "")
        # ej: dispen/+/status
        topic = f"{TOPIC_PREFIX}+/status"
        client.subscribe(topic)
        print("Suscripto a", topic)

def on_mqtt_message(client, userdata, msg):
    topic = msg.topic
    payload = msg.payload.decode("utf-8", errors="ignore")
    print("MQTT msg", topic, payload)
    if topic.endswith("/status"):
        try:
            data = json.loads(payload)
        except Exception:
            data = {}
        device = data.get("device")
        status = data.get("status")
        if not device or status not in ("online", "offline"):
            return
        online = (status == "online")
        with app.app_context():
            disp = Dispenser.query.filter_by(device_id=device).first()
            if not disp:
                # Si no existe, lo creamos "fantasma" para que aparezca
                disp = Dispenser(device_id=device, nombre=device)
                db.session.add(disp)
            if disp.online != online:
                disp.online = online
                db.session.commit()
                send_telegram(f"Dispenser {device} ahora está {'ONLINE' if online else 'OFFLINE'}.")

mqtt_client.on_connect = on_mqtt_connect
mqtt_client.on_message = on_mqtt_message

def start_mqtt():
    if not MQTT_HOST:
        print("MQTT deshabilitado (sin MQTT_HOST)")
        return
    try:
        mqtt_client.connect(MQTT_HOST, MQTT_PORT, 60)
        mqtt_client.loop_start()
        print("MQTT loop iniciado")
    except Exception as e:
        print("Error conectando MQTT:", e)


# arrancar MQTT en thread aparte al iniciar el servidor
threading.Thread(target=start_mqtt, daemon=True).start()

def publish_cmd_dispense(dispenser: Dispenser, tipo: str, pago_id: str):
    if not dispenser or not dispenser.device_id:
        return
    topic = f"{TOPIC_PREFIX}{dispenser.device_id}/cmd/dispense"
    payload = {
        "tipo": tipo,
        "pago_id": str(pago_id),
        "tiempo_segundos": dispenser.tiempo or 23
    }
    try:
        mqtt_client.publish(topic, json.dumps(payload), qos=1)
        print("Publicado CMD DISPENSE", topic, payload)
    except Exception as e:
        print("Error publicando MQTT:", e)

# ============================================================
#   MERCADOPAGO – PREFERENCIAS Y WEBHOOK
# ============================================================

def crear_preferencia_mp(dispenser: Dispenser, tipo: str):
    if not MP_ACCESS_TOKEN:
        raise RuntimeError("MP_ACCESS_TOKEN no configurado")

    if tipo == "fria":
        titulo = f"Agua Fría {dispenser.nombre or dispenser.device_id}"
        precio = dispenser.precio_frio
    else:
        titulo = f"Agua Caliente {dispenser.nombre or dispenser.device_id}"
        precio = dispenser.precio_caliente

    if precio is None or precio <= 0:
        raise RuntimeError("Precio inválido")

    external_reference = f"disp:{dispenser.id}:{tipo}"

    url = "https://api.mercadopago.com/checkout/preferences"
    headers = {
        "Authorization": f"Bearer {MP_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    body = {
        "items": [
            {
                "title": titulo,
                "quantity": 1,
                "currency_id": "ARS",
                "unit_price": float(precio),
            }
        ],
        "external_reference": external_reference,
        "notification_url": os.getenv("MP_WEBHOOK_URL", "") or "",  # opcional, MP también usa la de la app
    }

    resp = requests.post(url, headers=headers, json=body, timeout=15)
    if resp.status_code >= 300:
        print("Error MP:", resp.status_code, resp.text)
        raise RuntimeError(f"MercadoPago error {resp.status_code}")
    data = resp.json()
    # init_point es el link de pago para producción, sandbox_init_point para sandbox
    return data.get("init_point") or data.get("sandbox_init_point")

def obtener_pago_mp(payment_id: str):
    if not MP_ACCESS_TOKEN:
        raise RuntimeError("MP_ACCESS_TOKEN no configurado")
    url = f"https://api.mercadopago.com/v1/payments/{payment_id}"
    headers = {
        "Authorization": f"Bearer {MP_ACCESS_TOKEN}"
    }
    resp = requests.get(url, headers=headers, timeout=15)
    if resp.status_code >= 300:
        print("Error obteniendo pago:", resp.status_code, resp.text)
        raise RuntimeError("Error consultando pago")
    return resp.json()

@app.route("/webhook/mercadopago", methods=["POST", "GET"])
def webhook_mercadopago():
    # MP a veces manda GET para ver si está vivo
    if request.method == "GET":
        return "OK", 200

    data = request.json or {}
    print("Webhook MP:", data)

    payment_id = None

    # Formato típico: { "type":"payment", "data":{ "id":"123" } }
    if isinstance(data, dict):
        if "data" in data and isinstance(data["data"], dict):
            payment_id = data["data"].get("id")
        if not payment_id and "id" in data:
            payment_id = data.get("id")

    if not payment_id:
        # también puede venir por querystring
        payment_id = request.args.get("id") or request.args.get("data.id")

    if not payment_id:
        return jsonify({"error": "no_payment_id"}), 400

    try:
        pago = obtener_pago_mp(payment_id)
    except Exception as e:
        print("Error consultando pago:", e)
        return jsonify({"error": "fetch_payment_failed"}), 500

    status = pago.get("status")
    ext_ref = pago.get("external_reference", "")
    amount = pago.get("transaction_amount", 0)

    print("Pago MP status=", status, "external_reference=", ext_ref)

    # external_reference = "disp:<id_dispenser>:<tipo>"
    disp_id = None
    tipo = None
    try:
        parts = ext_ref.split(":")
        if len(parts) >= 3 and parts[0] == "disp":
            disp_id = int(parts[1])
            tipo = parts[2]
    except Exception:
        pass

    if not disp_id or tipo not in ("fria", "caliente"):
        return jsonify({"error": "bad_external_reference"}), 400

    with app.app_context():
        disp = Dispenser.query.get(disp_id)
        if not disp:
            return jsonify({"error": "dispenser_not_found"}), 404

        # Guardamos pago
        pay = Payment(
            mp_id=str(payment_id),
            dispenser_id=disp.id,
            tipo=tipo,
            amount=float(amount or 0),
            status=status,
        )
        db.session.add(pay)
        db.session.commit()

        if status == "approved":
            publish_cmd_dispense(disp, tipo, payment_id)
            send_telegram(f"Pago aprobado para {tipo} en {disp.nombre or disp.device_id}: ${amount}")
        else:
            send_telegram(f"Pago {status} para {tipo} en {disp.nombre or disp.device_id}: ${amount}")

    return jsonify({"ok": True})

# ============================================================
#   API ADMIN (para AdminPanel)
# ============================================================

@app.route("/api/admin/dispensers", methods=["GET"])
def api_list_dispensers():
    if not require_admin():
        return jsonify({"error": "unauthorized"}), 403
    q = Dispenser.query.order_by(Dispenser.id.asc()).all()
    return jsonify([
        {
            "id": d.id,
            "device_id": d.device_id,
            "nombre": d.nombre,
            "ubicacion": d.ubicacion,
            "tiempo": d.tiempo,
            "precio_frio": d.precio_frio,
            "precio_caliente": d.precio_caliente,
            "online": d.online,
        }
        for d in q
    ])

@app.route("/api/admin/dispensers", methods=["POST"])
def api_create_dispenser():
    if not require_admin():
        return jsonify({"error": "unauthorized"}), 403
    data = request.json or {}
    device_id = (data.get("device_id") or "").strip()
    if not device_id:
        return jsonify({"error": "device_id_required"}), 400

    if Dispenser.query.filter_by(device_id=device_id).first():
        return jsonify({"error": "device_id_exists"}), 400

    d = Dispenser(
        device_id=device_id,
        nombre=data.get("nombre") or device_id,
        ubicacion=data.get("ubicacion") or "",
        tiempo=int(data.get("tiempo") or 23),
        precio_frio=float(data.get("precio_frio") or 100),
        precio_caliente=float(data.get("precio_caliente") or 150),
    )
    db.session.add(d)
    db.session.commit()
    return jsonify({"ok": True, "id": d.id})

@app.route("/api/admin/dispensers/<int:disp_id>", methods=["PUT"])
def api_update_dispenser(disp_id):
    if not require_admin():
        return jsonify({"error": "unauthorized"}), 403
    data = request.json or {}
    d = Dispenser.query.get(disp_id)
    if not d:
        return jsonify({"error": "not_found"}), 404

    d.nombre = data.get("nombre", d.nombre)
    d.ubicacion = data.get("ubicacion", d.ubicacion)
    if "tiempo" in data:
        try:
            d.tiempo = int(data["tiempo"])
        except Exception:
            pass
    if "precio_frio" in data:
        try:
            d.precio_frio = float(data["precio_frio"])
        except Exception:
            pass
    if "precio_caliente" in data:
        try:
            d.precio_caliente = float(data["precio_caliente"])
        except Exception:
            pass

    db.session.commit()
    return jsonify({"ok": True})

@app.route("/api/admin/dispensers/<int:disp_id>/generar_qr_fria", methods=["POST"])
def api_qr_fria(disp_id):
    if not require_admin():
        return jsonify({"error": "unauthorized"}), 403
    d = Dispenser.query.get(disp_id)
    if not d:
        return jsonify({"error": "not_found"}), 404
    try:
        url = crear_preferencia_mp(d, "fria")
    except Exception as e:
        print("Error pref MP:", e)
        return jsonify({"error": "mp_error", "detail": str(e)}), 500
    return jsonify({"url": url})

@app.route("/api/admin/dispensers/<int:disp_id>/generar_qr_caliente", methods=["POST"])
def api_qr_caliente(disp_id):
    if not require_admin():
        return jsonify({"error": "unauthorized"}), 403
    d = Dispenser.query.get(disp_id)
    if not d:
        return jsonify({"error": "not_found"}), 404
    try:
        url = crear_preferencia_mp(d, "caliente")
    except Exception as e:
        print("Error pref MP:", e)
        return jsonify({"error": "mp_error", "detail": str(e)}), 500
    return jsonify({"url": url})

# ============================================================
#   RUTAS BÁSICAS
# ============================================================

@app.route("/")
def index():
    return "Dispen-Easy backend 2 salidas OK"

@app.route("/health")
def health():
    return jsonify({"status": "ok"})

# ============================================================
#   MAIN
# ============================================================

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
