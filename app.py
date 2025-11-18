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

db_url = os.getenv("DATABASE_URL", "sqlite:///dispen_easy.db")
# Fix típico de Railway para Postgres
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

# ============================================================
# MERCADOPAGO
# ============================================================

mp_access_token = os.getenv("MP_ACCESS_TOKEN", "")
sdk = mercadopago.SDK(mp_access_token)

# ============================================================
# TELEGRAM (por variables de entorno)
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
# MQTT CONFIG (HiveMQ Cloud, también configurable)
# ============================================================

MQTT_HOST = os.getenv("MQTT_HOST", "c9b4a2b821ce4be8710ed8e0ace8e4ee.s1.eu.hivemq.cloud")
MQTT_PORT = int(os.getenv("MQTT_PORT", "8883"))
MQTT_USER = os.getenv("MQTT_USER", "Dispen")
MQTT_PASS = os.getenv("MQTT_PASS", "dispen2025")

# Wildcards para multi-dispenser
TOPIC_ACK_WILDCARD    = "dispen/+/state/dispense"
TOPIC_STATUS_WILDCARD = "dispen/+/status"

# ============================================================
# MODELOS
# ============================================================

class Dispenser(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    device_id = db.Column(db.String(50), unique=True, nullable=False)  # ej: "dispen-01"
    nombre = db.Column(db.String(100), nullable=False, default="Dispenser")
    ubicacion = db.Column(db.String(200))
    tiempo_segundos = db.Column(db.Integer, default=23)

    online = db.Column(db.Boolean, default=False)
    last_seen = db.Column(db.DateTime)

    productos = db.relationship("Producto", backref="dispenser", lazy=True)
    pagos = db.relationship("Pago", backref="dispenser", lazy=True)


class Producto(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    dispenser_id = db.Column(db.Integer, db.ForeignKey('dispenser.id'), nullable=False)
    nombre = db.Column(db.String(50), nullable=False)  # "Agua fría" / "Agua caliente"
    tipo = db.Column(db.String(20), nullable=False)    # "fria" o "caliente"
    precio = db.Column(db.Float, nullable=False)


class Pago(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    mp_payment_id = db.Column(db.String(50), unique=True)
    dispenser_id = db.Column(db.Integer, db.ForeignKey('dispenser.id'))
    producto_id = db.Column(db.Integer, db.ForeignKey('producto.id'))
    estado = db.Column(db.String(20), default="pendiente")
    fecha = db.Column(db.DateTime, default=datetime.utcnow)


# ============================================================
# MQTT CALLBACKS
# ============================================================

def _extract_device_id_from_topic(topic: str) -> str:
    """
    Topics vienen como:
    dispen/<device_id>/state/dispense
    dispen/<device_id>/status
    """
    parts = topic.split("/")
    if len(parts) >= 3:
        return parts[1]
    return ""


def on_mqtt_message(client, userdata, msg):
    payload = msg.payload.decode()
    topic = msg.topic
    print(f"[MQTT] Mensaje en {topic}: {payload}")

    device_id = _extract_device_id_from_topic(topic)

    with app.app_context():
        # ACK DISPENSADO
        if topic.startswith("dispen/") and topic.endswith("/state/dispense"):
            try:
                data = json.loads(payload)
                pago_id = data.get("pago_id")
                dispensado = data.get("dispensado")

                if dispensado and pago_id:
                    pago = Pago.query.get(pago_id)
                    if not pago:
                        print("[MQTT] Pago no encontrado:", pago_id)
                        return

                    dispenser = Dispenser.query.get(pago.dispenser_id)
                    producto = Producto.query.get(pago.producto_id)

                    pago.estado = "dispensado"
                    db.session.commit()

                    nombre_disp = dispenser.nombre if dispenser else device_id

                    send_telegram(f"""
✔ <b>DISPENSADO COMPLETADO</b>

Dispenser: <b>{nombre_disp}</b> (ID: <code>{device_id}</code>)
Producto: <b>{producto.nombre}</b>
GPIO lógico: salida tipo <b>{producto.tipo}</b>
Tiempo estimado: <b>{dispenser.tiempo_segundos if dispenser else 23} s</b>

Pago interno: <code>{pago_id}</code>
📅 {datetime.utcnow().strftime("%d/%m/%Y %H:%M:%S")}
""")
            except Exception as e:
                print("[ERROR ACK]:", e)

        # ESTADO ONLINE/OFFLINE DEL ESP32
        elif topic.startswith("dispen/") and topic.endswith("/status"):
            estado = payload.lower().strip()
            if not device_id:
                return

            dispenser = Dispenser.query.filter_by(device_id=device_id).first()
            if not dispenser:
                # Si no existe, lo creamos mínimo en la BD
                dispenser = Dispenser(
                    device_id=device_id,
                    nombre=f"Dispenser {device_id}",
                    tiempo_segundos=23
                )
                db.session.add(dispenser)
                db.session.commit()

            dispenser.last_seen = datetime.utcnow()
            if estado == "online":
                dispenser.online = True
                send_telegram(f"🟢 <b>{dispenser.nombre}</b> (ID: <code>{device_id}</code>) ONLINE")
            elif estado == "offline":
                dispenser.online = False
                send_telegram(f"🔴 <b>{dispenser.nombre}</b> (ID: <code>{device_id}</code>) OFFLINE")

            db.session.commit()


def on_mqtt_connect(client, userdata, flags, reason_code, properties=None):
    print("[MQTT] Conectado ✔")
    client.subscribe(TOPIC_ACK_WILDCARD)
    client.subscribe(TOPIC_STATUS_WILDCARD)


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
# INICIALIZACIÓN (Flask 3 compatible)
# ============================================================

def inicializar():
    with app.app_context():
        db.create_all()
        # NO creamos dispensers ni productos aquí: se hace desde el panel admin.


inicializar()

send_telegram("🟢 <b>DISPEN-EASY BACKEND ONLINE</b>\nServidor iniciado correctamente.")


# ============================================================
# ENDPOINTS ADMIN PARA DISPENSERS / PRODUCTOS
# ============================================================

@app.route("/api/dispensers", methods=["GET"])
def listar_dispensers():
    ds = Dispenser.query.all()
    return jsonify([
        {
            "id": d.id,
            "device_id": d.device_id,
            "nombre": d.nombre,
            "ubicacion": d.ubicacion,
            "tiempo_segundos": d.tiempo_segundos,
            "online": d.online,
            "last_seen": d.last_seen.isoformat() if d.last_seen else None
        } for d in ds
    ])


@app.route("/api/dispensers", methods=["POST"])
def crear_dispenser():
    """
    Crea un dispenser y automáticamente sus 2 productos (fría / caliente).

    JSON esperado:
    {
      "device_id": "dispen-01",
      "nombre": "Dispenser Plaza",
      "ubicacion": "Plaza central",
      "tiempo_segundos": 23,
      "precio_fria": 100.0,
      "precio_caliente": 150.0
    }
    """
    data = request.json or {}

    device_id = data.get("device_id")
    if not device_id:
        return jsonify({"error": "device_id requerido"}), 400

    if Dispenser.query.filter_by(device_id=device_id).first():
        return jsonify({"error": "device_id ya existe"}), 400

    nombre = data.get("nombre", f"Dispenser {device_id}")
    ubicacion = data.get("ubicacion", "")
    tiempo_segundos = data.get("tiempo_segundos", 23)
    precio_fria = data.get("precio_fria", 100.0)
    precio_caliente = data.get("precio_caliente", 150.0)

    d = Dispenser(
        device_id=device_id,
        nombre=nombre,
        ubicacion=ubicacion,
        tiempo_segundos=tiempo_segundos
    )
    db.session.add(d)
    db.session.commit()

    # Crear productos asociados a este dispenser
    prod_fria = Producto(dispenser_id=d.id, nombre="Agua fría", tipo="fria", precio=precio_fria)
    prod_cal = Producto(dispenser_id=d.id, nombre="Agua caliente", tipo="caliente", precio=precio_caliente)
    db.session.add(prod_fria)
    db.session.add(prod_cal)
    db.session.commit()

    return jsonify({"ok": True, "dispenser_id": d.id})


@app.route("/api/dispensers/<int:disp_id>", methods=["PATCH"])
def actualizar_dispenser(disp_id):
    """
    Permite actualizar tiempo y precios por dispenser.

    JSON ejemplo:
    {
      "tiempo_segundos": 25,
      "precio_fria": 120.0,
      "precio_caliente": 180.0,
      "nombre": "Dispenser Nuevo Nombre",
      "ubicacion": "Lugar X"
    }
    """
    d = Dispenser.query.get(disp_id)
    if not d:
        return jsonify({"error": "dispenser no encontrado"}), 404

    data = request.json or {}

    if "tiempo_segundos" in data:
        d.tiempo_segundos = data["tiempo_segundos"]
    if "nombre" in data:
        d.nombre = data["nombre"]
    if "ubicacion" in data:
        d.ubicacion = data["ubicacion"]

    # Actualizar precios de productos
    prod_fria = Producto.query.filter_by(dispenser_id=d.id, tipo="fria").first()
    prod_cal = Producto.query.filter_by(dispenser_id=d.id, tipo="caliente").first()

    if "precio_fria" in data and prod_fria:
        prod_fria.precio = data["precio_fria"]
    if "precio_caliente" in data and prod_cal:
        prod_cal.precio = data["precio_caliente"]

    db.session.commit()

    return jsonify({"ok": True})


@app.route("/api/dispensers/<int:disp_id>/productos", methods=["GET"])
def listar_productos_dispenser(disp_id):
    d = Dispenser.query.get(disp_id)
    if not d:
        return jsonify({"error": "dispenser no encontrado"}), 404

    return jsonify([
        {
            "id": p.id,
            "nombre": p.nombre,
            "tipo": p.tipo,
            "precio": p.precio
        } for p in d.productos
    ])


# ============================================================
# GENERAR QR MERCADOPAGO (por dispenser y tipo)
# ============================================================

@app.route("/api/generar_qr/<int:disp_id>/<string:tipo>", methods=["POST"])
def generar_qr(disp_id, tipo):
    """
    Genera QR para un dispenser y tipo de producto ("fria" o "caliente").
    """
    d = Dispenser.query.get(disp_id)
    if not d:
        return jsonify({"error": "dispenser no encontrado"}), 404

    p = Producto.query.filter_by(dispenser_id=d.id, tipo=tipo).first()
    if not p:
        return jsonify({"error": "producto no existe para ese dispenser"}), 404

    # external_reference: "disp_id:prod_id"
    external_reference = f"{d.id}:{p.id}"

    pref = {
        "items": [{
            "title": p.nombre,
            "quantity": 1,
            "currency_id": "ARS",
            "unit_price": p.precio
        }],
        "notification_url": request.host_url + "webhook",
        "external_reference": external_reference
    }

    r = sdk.preference().create(pref)
    return jsonify({
        "url_pago": r["response"]["init_point"],
        "dispenser_id": d.id,
        "producto_id": p.id,
        "external_reference": external_reference
    })


# ============================================================
# WEBHOOK MERCADOPAGO
# ============================================================

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json or {}

    if data.get("type") == "payment":
        payment_id = data["data"]["id"]

        info = sdk.payment().get(payment_id)["response"]
        status = info["status"]
        external_reference = info.get("external_reference", "")

        # external_reference formateado como "disp_id:prod_id"
        try:
            disp_id_str, prod_id_str = external_reference.split(":")
            disp_id = int(disp_id_str)
            prod_id = int(prod_id_str)
        except Exception:
            print("[WEBHOOK] external_reference inválido:", external_reference)
            return "ok", 200

        dispenser = Dispenser.query.get(disp_id)
        producto = Producto.query.get(prod_id)
        if not dispenser or not producto:
            print("[WEBHOOK] Dispenser o producto no encontrados")
            return "ok", 200

        # Evitar duplicado
        if Pago.query.filter_by(mp_payment_id=payment_id).first():
            return "ok", 200

        pago = Pago(
            mp_payment_id=payment_id,
            dispenser_id=dispenser.id,
            producto_id=producto.id,
            estado="aprobado" if status == "approved" else "pendiente"
        )
        db.session.add(pago)
        db.session.commit()

        if status == "approved":
            send_telegram(f"""
🟢 <b>PAGO APROBADO</b>

Dispenser: <b>{dispenser.nombre}</b> (ID: <code>{dispenser.device_id}</code>)
Producto: <b>{producto.nombre}</b>
Monto: <b>${producto.precio}</b>

Estado: <b>Esperando botón</b>

Pago MP: <code>{payment_id}</code>
Pago interno: <code>{pago.id}</code>
⏱ Tiempo programado: <b>{dispenser.tiempo_segundos} s</b>
📅 {datetime.utcnow().strftime("%d/%m/%Y %H:%M:%S")}
""")

            topic_cmd = f"dispen/{dispenser.device_id}/cmd/dispense"
            mqtt_client.publish(topic_cmd, json.dumps({
                "cmd": "dispensar",
                "producto_id": producto.id,
                "tipo": producto.tipo,
                "gpio_salida": 1 if producto.tipo == "fria" else 2,  # lógica simple
                "pago_id": pago.id,
                "tiempo_segundos": dispenser.tiempo_segundos
            }))

    return "ok", 200


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
