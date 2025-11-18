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
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///dispen_easy.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

# ============================================================
# MERCADOPAGO
# ============================================================

mp_access_token = os.getenv("MP_ACCESS_TOKEN", "")
sdk = mercadopago.SDK(mp_access_token)

# ============================================================
# TELEGRAM
# ============================================================

TELEGRAM_TOKEN   = "TU_TOKEN"
TELEGRAM_CHAT_ID = "TU_CHAT_ID"

def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": msg,
        "parse_mode": "HTML"
    }
    try:
        requests.post(url, data=data)
    except:
        print("[ERROR] No se pudo enviar mensaje Telegram")

# ============================================================
# MQTT CONFIG (HiveMQ Cloud)
# ============================================================

MQTT_HOST = "c9b4a2b821ce4be8710ed8e0ace8e4ee.s1.eu.hivemq.cloud"
MQTT_PORT = 8883
MQTT_USER = "Dispen"
MQTT_PASS = "dispen2025"

DEVICE_ID = "dispen-01"

TOPIC_CMD  = f"dispen/{DEVICE_ID}/cmd/dispense"
TOPIC_ACK  = f"dispen/{DEVICE_ID}/state/dispense"

# ============================================================
# MODELOS
# ============================================================

class Producto(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(50), nullable=False)
    precio = db.Column(db.Float, nullable=False)
    gpio_salida = db.Column(db.Integer, nullable=False)  # 1=fría, 2=caliente

class Pago(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    mp_payment_id = db.Column(db.String(50), unique=True)
    producto_id = db.Column(db.Integer, db.ForeignKey('producto.id'))
    estado = db.Column(db.String(20), default="pendiente")
    fecha = db.Column(db.DateTime, default=datetime.utcnow)

# ============================================================
# MQTT CALLBACK (ACK desde ESP32)
# ============================================================

def on_mqtt_message(client, userdata, msg):
    print("[MQTT] ACK recibido:", msg.payload.decode())
    try:
        data = json.loads(msg.payload.decode())

        pago_id = data.get("pago_id")
        dispensado = data.get("dispensado")

        if dispensado and pago_id:
            pago = Pago.query.get(pago_id)
            producto = Producto.query.get(pago.producto_id)

            pago.estado = "dispensado"
            db.session.commit()

            mensaje = f"""
✔ <b>DISPENSADO COMPLETADO</b>

Producto: <b>{producto.nombre}</b>
Salida GPIO: <b>{producto.gpio_salida}</b>

⏱ Tiempo real: <b>23 s</b>
Aviso final: <b>Parpadeo rápido (20–23s)</b>

Pago interno: <code>{pago_id}</code>

📅 {datetime.utcnow().strftime('%d/%m/%Y %H:%M:%S')}
"""
            send_telegram(mensaje)
            print("[MQTT] Pago marcado como DISPENSADO ✔")

    except Exception as e:
        print("[ERROR MQTT]:", e)

mqtt_client = mqtt.Client()
mqtt_client.username_pw_set(MQTT_USER, MQTT_PASS)
mqtt_client.tls_set()
mqtt_client.on_message = on_mqtt_message
mqtt_client.connect(MQTT_HOST, MQTT_PORT, 60)
mqtt_client.subscribe(TOPIC_ACK)
mqtt_client.loop_start()

# ============================================================
# INICIALIZACION
# ============================================================

@app.before_first_request
def inicializar():
    db.create_all()

    if Producto.query.count() == 0:
        db.session.add(Producto(nombre="Agua fría", precio=100.0, gpio_salida=1))
        db.session.add(Producto(nombre="Agua caliente", precio=150.0, gpio_salida=2))
        db.session.commit()

# ============================================================
# LISTAR PRODUCTOS
# ============================================================

@app.route("/api/productos")
def lista_productos():
    prods = Producto.query.all()
    return jsonify([
        {
            "id": p.id,
            "nombre": p.nombre,
            "precio": p.precio,
            "gpio_salida": p.gpio_salida
        } for p in prods
    ])

# ============================================================
# GENERAR QR MERCADOPAGO
# ============================================================

@app.route("/api/generar_qr/<int:id_prod>", methods=["POST"])
def generar_qr(id_prod):
    p = Producto.query.get(id_prod)
    if not p:
        return jsonify({"error": "producto no existe"}), 404

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

# ============================================================
# WEBHOOK MERCADOPAGO
# ============================================================

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

        pago = Pago(
            mp_payment_id=payment_id,
            producto_id=producto.id,
            estado="aprobado" if status == "approved" else "pendiente"
        )
        db.session.add(pago)
        db.session.commit()

        # --- TELEGRAM notificando pago aprobado ---
        if status == "approved":
            mensaje = f"""
🟢 <b>PAGO APROBADO</b>

Producto: <b>{producto.nombre}</b>
Salida GPIO: <b>{producto.gpio_salida}</b>
Monto: <b>${producto.precio}</b>

Estado: <b>Esperando que presione el botón</b>

Pago MP: <code>{payment_id}</code>
Pago interno: <code>{pago.id}</code>

⏱ Tiempo programado: <b>23 s</b>
📅 {datetime.utcnow().strftime('%d/%m/%Y %H:%M:%S')}
"""
            send_telegram(mensaje)

            # --- Enviar comando MQTT al ESP32 ---
            msg = {
                "cmd": "dispensar",
                "producto_id": producto.id,
                "gpio_salida": producto.gpio_salida,
                "pago_id": pago.id
            }
            mqtt_client.publish(TOPIC_CMD, json.dumps(msg))
            print("[MQTT] Enviado comando a ESP32:", msg)

    return "ok", 200

# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
