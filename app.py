import os
from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
import requests
import mercadopago
import json
import paho.mqtt.client as mqtt

# ============================================================
# CONFIG FLASK + DB
# ============================================================

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "sqlite:///dispen_easy.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

# ============================================================
# MERCADOPAGO
# ============================================================

mp_access_token = os.getenv("MP_ACCESS_TOKEN", "")
sdk = mercadopago.SDK(mp_access_token)

# ============================================================
# TELEGRAM (LEE VARIABLES DE RAILWAY)
# ============================================================

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

def send_telegram(msg: str):
    """Envía mensaje a Telegram usando variables del entorno."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] Telegram no está configurado")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}
    try:
        requests.post(url, data=data, timeout=5)
    except Exception as e:
        print("[ERROR] No se pudo enviar mensaje Telegram:", e)

# ============================================================
# MQTT CONFIG (HiveMQ Cloud) – también con variables
# ============================================================

MQTT_HOST = os.getenv("MQTT_HOST", "c9b4a2b821ce4be8710ed8e0ace8e4ee.s1.eu.hivemq.cloud")
MQTT_PORT = int(os.getenv("MQTT_PORT", "8883"))
MQTT_USER = os.getenv("MQTT_USER", "Dispen")
MQTT_PASS = os.getenv("MQTT_PASS", "dispen2025")

DEVICE_ID = os.getenv("DEVICE_ID", "dispen-01")

TOPIC_CMD    = f"dispen/{DEVICE_ID}/cmd/dispense"
TOPIC_ACK    = f"dispen/{DEVICE_ID}/state/dispense"
TOPIC_STATUS = f"dispen/{DEVICE_ID}/status"

# ============================================================
# MODELOS DE BD
# ============================================================

class Producto(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(50), nullable=False)
    precio = db.Column(db.Float, nullable=False)
    gpio_salida = db.Column(db.Integer, nullable=False)

class Pago(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    mp_payment_id = db.Column(db.String(50), unique=True)
    producto_id = db.Column(db.Integer, db.ForeignKey('producto.id'))
    estado = db.Column(db.String(20), default="pendiente")
    fecha = db.Column(db.DateTime, default=datetime.utcnow)

# ============================================================
# MQTT CALLBACKS
# ============================================================

def on_mqtt_message(client, userdata, msg):
    payload = msg.payload.decode()
    print(f"[MQTT] Mensaje en {msg.topic}: {payload}")

    with app.app_context():

        # -------------------------
        # ACK DEL ESP32
        # -------------------------
        if msg.topic == TOPIC_ACK:
            try:
                data = json.loads(payload)
                pago_id = data.get("pago_id")
                dispensado = data.get("dispensado")

                if dispensado and pago_id:
                    pago = Pago.query.get(pago_id)
                    if not pago:
                        return
                    producto = Producto.query.get(pago.producto_id)
                    pago.estado = "dispensado"
                    db.session.commit()

                    send_telegram(f"""
✔ <b>DISPENSADO COMPLETADO</b>

Producto: <b>{producto.nombre}</b>
GPIO: <b>{producto.gpio_salida}</b>
Tiempo real: 23s

Pago interno: <code>{pago_id}</code>
📅 {datetime.utcnow().strftime("%d/%m/%Y %H:%M:%S")}
""")
            except Exception as e:
                print("[ERROR ACK]:", e)

        # -------------------------
        # ESTADO DEL ESP32
        # -------------------------
        elif msg.topic == TOPIC_STATUS:
            estado = payload.lower().strip()
            if estado == "online":
                send_telegram("🟢 <b>ESP32 DISPEN-01 ONLINE</b>")
            elif estado == "offline":
                send_telegram("🔴 <b>ESP32 DISPEN-01 OFFLINE</b>")

def on_mqtt_connect(client, userdata, flags, reason_code, properties=None):
    print("[MQTT] Conectado ✔")
    client.subscribe(TOPIC_ACK)
    client.subscribe(TOPIC_STATUS)

def on_mqtt_disconnect(client, userdata, reason_code, properties=None):
    print("[MQTT] Desconectado:", reason_code)

mqtt_client = mqtt.Client()
mqtt_client.username_pw_set(MQTT_USER, MQTT_PASS)
mqtt_client.tls_set()
mqtt_client.on_connect = on_mqtt_connect
mqtt_client.on_disconnect = on_mqtt_disconnect
mqtt_client.on_message = on_mqtt_message
mqtt_client.connect(MQTT_HOST, MQTT_PORT, 60)
mqtt_client.loop_start()

# ============================================================
# INICIALIZACIÓN SEGURA (Flask 3.x compatible)
# ============================================================

def inicializar():
    with app.app_context():
        db.create_all()
        if Producto.query.count() == 0:
            db.session.add(Producto(nombre="Agua fría", precio=100.0, gpio_salida=1))
            db.session.add(Producto(nombre="Agua caliente", precio=150.0, gpio_salida=2))
            db.session.commit()

inicializar()

# Al iniciar el backend → enviar ONLINE
send_telegram("🟢 <b>DISPEN-EASY BACKEND ONLINE</b>\nServidor iniciado correctamente.")

# ============================================================
# ENDPOINTS
# ============================================================

@app.route("/api/productos")
def productos():
    prods = Producto.query.all()
    return jsonify([{ 
        "id": p.id, 
        "nombre": p.nombre,
        "precio": p.precio,
        "gpio_salida": p.gpio_salida
    } for p in prods])

@app.route("/api/generar_qr/<int:id_prod>", methods=["POST"])
def generar_qr(id_prod):
    p = Producto.query.get(id_prod)
    if not p: return jsonify({"error": "producto no existe"}), 404

    pref = {
        "items": [{
            "title": p.nombre,
            "quantity": 1,
            "currency_id": "ARS",
            "unit_price": p.precio
        }],
        "notification_url": request.host_url + "webhook"
    }

    r = sdk.preference().create(pref)
    return jsonify({"url_pago": r["response"]["init_point"]})

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json

    if data.get("type") == "payment":
        payment_id = data["data"]["id"]
        info = sdk.payment().get(payment_id)["response"]

        status = info["status"]
        descripcion = info.get("description", "")

        producto = Producto.query.filter_by(nombre=descripcion).first()
        if not producto:
            return "ok", 200

        if Pago.query.filter_by(mp_payment_id=payment_id).first():
            return "ok", 200

        pago = Pago(mp_payment_id=payment_id,
                    producto_id=producto.id,
                    estado="aprobado" if status == "approved" else "pendiente")
        db.session.add(pago)
        db.session.commit()

        if status == "approved":
            send_telegram(f"""
🟢 <b>PAGO APROBADO</b>

Producto: <b>{producto.nombre}</b>
GPIO: <b>{producto.gpio_salida}</b>
Monto: <b>${producto.precio}</b>

Estado: <b>Esperando botón</b>

Pago MP: <code>{payment_id}</code>
Pago interno: <code>{pago.id}</code>
⏱ 23s
📅 {datetime.utcnow().strftime("%d/%m/%Y %H:%M:%S")}
""")

            mqtt_client.publish(TOPIC_CMD, json.dumps({
                "cmd": "dispensar",
                "producto_id": producto.id,
                "gpio_salida": producto.gpio_salida,
                "pago_id": pago.id
            }))

    return "ok", 200

# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
