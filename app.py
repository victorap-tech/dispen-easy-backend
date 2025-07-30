from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
import os
import datetime
import paho.mqtt.client as mqtt
import requests
import json
from decimal import Decimal

app = Flask(__name__)
CORS(app)

# Configuración base de datos
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get("DATABASE_URL", "sqlite:///./pagos.db")
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# Mercado Pago
MP_ACCESS_TOKEN = os.environ.get("MP_ACCESS_TOKEN")
FRONTEND_URL = os.environ.get("FRONTEND_URL", "https://dispen-easy-web-production.up.railway.app/")

# MQTT
MQTT_BROKER = os.environ.get("MQTT_BROKER_HOST", "c9b4a2b821ec4e87b10ed8e0ace8e4ee.s1.eu.hivemq.cloud")
MQTT_PORT = int(os.environ.get("MQTT_BROKER_PORT", 8883))
MQTT_USERNAME = os.environ.get("MQTT_BROKER_USERNAME", "Victor")
MQTT_PASSWORD = os.environ.get("MQTT_BROKER_PASSWORD", "Dispen2025")

mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
mqtt_client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print("MQTT conectado")
        client.subscribe("dispensador/status")
    else:
        print(f"MQTT error conexión: {rc}")

def on_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode())
        id_pago = payload.get("id_pago")
        estado = payload.get("estado")
        if id_pago and estado:
            with app.app_context():
                pago = Pago.query.filter_by(id_pago_mp=id_pago).first()
                if pago and estado == "DISPENSADO":
                    pago.dispensado = True
                    db.session.commit()
    except Exception as e:
        print("Error MQTT:", e)

mqtt_client.on_connect = on_connect
mqtt_client.on_message = on_message

try:
    mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
    mqtt_client.loop_start()
except Exception as e:
    print("Error al conectar MQTT:", e)

# Modelos
class Producto(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(100), nullable=False)
    cantidad_ml = db.Column(db.Integer, nullable=False)
    precio = db.Column(db.Numeric(10, 2), nullable=False)

class Pago(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    id_pago_mp = db.Column(db.String(255), unique=True, nullable=False)
    estado = db.Column(db.String(50), nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.datetime.now)
    dispensado = db.Column(db.Boolean, default=False)
    producto_id = db.Column(db.Integer, db.ForeignKey('producto.id'))
    producto = db.relationship('Producto')

# Inicializar base
def initialize_database():
    with app.app_context():
        db.create_all()
        if Producto.query.count() == 0:
            db.session.add_all([
                Producto(nombre="Lavandina", cantidad_ml=500, precio=Decimal("150.00")),
                Producto(nombre="Jabón Líquido", cantidad_ml=300, precio=Decimal("200.00"))
            ])
            db.session.commit()

# Rutas
@app.route("/")
def home():
    return jsonify({"message": "Backend Dispen-Easy operativo"})

@app.route("/api/productos", methods=["GET", "POST"])
def productos():
    if request.method == "GET":
        lista = Producto.query.all()
        return jsonify([
            {"id": p.id, "nombre": p.nombre, "precio": float(p.precio), "cantidad_ml": p.cantidad_ml}
            for p in lista
        ])
    elif request.method == "POST":
        data = request.json
        nuevo = Producto(
            nombre=data["nombre"],
            cantidad_ml=data.get("cantidad_ml", 500),
            precio=Decimal(str(data["precio"]))
        )
        db.session.add(nuevo)
        db.session.commit()
        return jsonify({"success": True, "message": "Producto agregado"})

@app.route("/api/productos/<int:producto_id>", methods=["DELETE"])
def eliminar_producto(producto_id):
    producto = Producto.query.get(producto_id)
    if not producto:
        return jsonify({"success": False, "message": "No encontrado"}), 404
    db.session.delete(producto)
    db.session.commit()
    return jsonify({"success": True, "message": "Producto eliminado"})

@app.route("/api/generar_qr/<int:producto_id>", methods=["POST"])
def generar_qr(producto_id):
    if not MP_ACCESS_TOKEN:
        return jsonify({"status": "error", "message": "Falta MP_ACCESS_TOKEN"}), 500

    producto = Producto.query.get(producto_id)
    if not producto:
        return jsonify({"status": "error", "message": "Producto no encontrado"}), 404

    external_reference = f"pago-{producto.id}-{datetime.datetime.now().strftime('%Y%m%d%H%M%S%f')}"
    preference = {
        "items": [{
            "title": producto.nombre,
            "quantity": 1,
            "unit_price": float(producto.precio)
        }],
        "external_reference": external_reference,
        "notification_url": f"{request.url_root}webhook_mp",
        "back_urls": {
            "success": FRONTEND_URL,
            "failure": FRONTEND_URL,
            "pending": FRONTEND_URL
        },
        "auto_return": "approved"
    }

    headers = {
        "Authorization": f"Bearer {MP_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }

    try:
        r = requests.post("https://api.mercadopago.com/checkout/preferences", headers=headers, json=preference)
        r.raise_for_status()
        data = r.json()
        return jsonify({"status": "success", "qr_data": data.get("init_point"), "id_pago_mp": data.get("id")})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/webhook_mp", methods=["POST"])
def webhook():
    data = request.get_json()
    if data.get("type") == "payment":
        payment_id = data.get("data", {}).get("id")
        headers = {"Authorization": f"Bearer {MP_ACCESS_TOKEN}"}
        try:
            r = requests.get(f"https://api.mercadopago.com/v1/payments/{payment_id}", headers=headers)
            r.raise_for_status()
            info = r.json()
            estado = info.get("status")
            ref = info.get("external_reference")
            producto_id = int(ref.split("-")[1]) if ref else None
            producto = Producto.query.get(producto_id) if producto_id else None

            existente = Pago.query.filter_by(id_pago_mp=payment_id).first()
            if not existente:
                nuevo = Pago(id_pago_mp=payment_id, estado=estado, producto=producto)
                db.session.add(nuevo)
            else:
                existente.estado = estado
            db.session.commit()

            if estado == "approved":
                mqtt_client.publish("dispensador/comando", json.dumps({
                    "id_pago": str(payment_id),
                    "comando": "DISPENSAR",
                    "producto_id": producto_id,
                    "cantidad_ml": producto.cantidad_ml if producto else 0
                }))
        except Exception as e:
            print("Error webhook:", e)
    return jsonify({"status": "ok"})

# Main
if __name__ == "__main__":
    initialize_database()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
