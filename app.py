import os
from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
import paho.mqtt.client as mqtt
import json

# =============================================================
#   CONFIG FLASK
# =============================================================

app = Flask(__name__)

# Habilitar CORS (FRONTEND + OTRAS APPS)
CORS(app, resources={r"/*": {"origins": "*"}})

# Base de datos SQLite (Railway lo maneja en archivo)
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///dispenders.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

# =============================================================
#   MODELO — DISPENSERS
# =============================================================

class Dispenser(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    device_id = db.Column(db.String(64), unique=True, nullable=False)
    nombre = db.Column(db.String(128), nullable=True)
    ubicacion = db.Column(db.String(128), nullable=True)
    tiempo_segundos = db.Column(db.Integer, default=23)
    precio_fria = db.Column(db.Integer, default=100)
    precio_caliente = db.Column(db.Integer, default=150)

# Crear tabla si no existe
with app.app_context():
    db.create_all()

# =============================================================
#   CONFIG — ADMIN SECRET
# =============================================================

ADMIN_SECRET = os.getenv("ADMIN_SECRET", "1234")

def require_admin(req):
    token = req.headers.get("x-admin-token", "")
    return token == ADMIN_SECRET

# =============================================================
#   MQTT CONFIG
# =============================================================

MQTT_HOST = os.getenv("MQTT_HOST")
MQTT_PORT = int(os.getenv("MQTT_PORT", 8883))
MQTT_USER = os.getenv("MQTT_USER")
MQTT_PASS = os.getenv("MQTT_PASS")
TOPIC_PREFIX = os.getenv("TOPIC_PREFIX", "dispen/")

mqtt_client = mqtt.Client()
mqtt_client.username_pw_set(MQTT_USER, MQTT_PASS)
mqtt_client.tls_set()   # TLS automático
mqtt_client.connect(MQTT_HOST, MQTT_PORT, 60)
mqtt_client.loop_start()

# =============================================================
#   ENDPOINTS — DISPENSERS
# =============================================================

@app.route("/api/dispensers", methods=["GET"])
def listar_dispensers():
    all = Dispenser.query.all()
    return jsonify([
        {
            "device_id": d.device_id,
            "nombre": d.nombre,
            "ubicacion": d.ubicacion,
            "tiempo_segundos": d.tiempo_segundos,
            "precio_fria": d.precio_fria,
            "precio_caliente": d.precio_caliente,
        }
        for d in all
    ])

@app.route("/api/dispensers", methods=["POST"])
def crear_dispenser():
    if not require_admin(request):
        return jsonify({"error": "unauthorized"}), 403

    data = request.json

    device_id = data.get("device_id", "").strip()
    if device_id == "":
        return jsonify({"error": "device_id_required"}), 400

    # Si ya existe → error
    if Dispenser.query.filter_by(device_id=device_id).first():
        return jsonify({"error": "device_id_exists"}), 400

    d = Dispenser(
        device_id=device_id,
        nombre=data.get("nombre", ""),
        ubicacion=data.get("ubicacion", ""),
        tiempo_segundos=int(data.get("tiempo_segundos", 23)),
        precio_fria=int(data.get("precio_fria", 100)),
        precio_caliente=int(data.get("precio_caliente", 150))
    )

    db.session.add(d)
    db.session.commit()

    return jsonify({"ok": True})

# =============================================================
#   ENDPOINT — ENVIAR COMANDO DISPENSAR
# =============================================================

@app.route("/api/dispensers/<device_id>/dispense", methods=["POST"])
def activar_dispense(device_id):
    if not require_admin(request):
        return jsonify({"error": "unauthorized"}), 403

    data = request.json
    tipo = data.get("tipo")            # "fria" o "caliente"
    pago_id = data.get("pago_id")
    tiempo = data.get("tiempo_segundos")

    cmd = {
        "cmd": "dispensar",
        "tipo": tipo,
        "pago_id": pago_id,
        "tiempo_segundos": tiempo
    }

    topic = f"{TOPIC_PREFIX}{device_id}/cmd/dispense"

    mqtt_client.publish(topic, json.dumps(cmd))

    return jsonify({"ok": True, "sent_to": topic})

# =============================================================
#   MAIN
# =============================================================

@app.route("/")
def home():
    return "Dispen-Easy backend OK"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
