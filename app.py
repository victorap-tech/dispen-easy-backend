from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
import os
import paho.mqtt.client as mqtt
import requests

app = Flask(__name__)
CORS(app)

# Base de datos: usa DATABASE_URL si está definido, si no usa SQLite local
DATABASE_URL = os.getenv('DATABASE_URL', 'sqlite:///pagos.db')
app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# Modelos
class Producto(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(100), nullable=False)
    precio = db.Column(db.Float, nullable=False)
    cantidad_litros = db.Column(db.Float, nullable=False)

class Pago(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    payment_id = db.Column(db.String(100), unique=True)
    producto_id = db.Column(db.Integer, db.ForeignKey('producto.id'))
    dispensado = db.Column(db.Boolean, default=False)

# MQTT
mqtt_client = mqtt.Client()
mqtt_broker = os.getenv("MQTT_BROKER", "c9b4a2b821ec4e64b81628b63d4f452c.s1.eu.hivemq.cloud")
mqtt_port = int(os.getenv("MQTT_PORT", 8883))
mqtt_user = os.getenv("MQTT_USER", "Victor")
mqtt_pass = os.getenv("MQTT_PASS", "Dispeneasy2025")

if mqtt_user and mqtt_pass:
    mqtt_client.username_pw_set(mqtt_user, mqtt_pass)
    mqtt_client.tls_set()  # Habilita TLS si es HiveMQ Cloud
    mqtt_client.connect(mqtt_broker, mqtt_port, 60)

# Rutas
@app.route('/')
def index():
    return 'Backend Dispen-Easy funcionando'

@app.route('/api/productos', methods=['GET'])
def listar_productos():
    productos = Producto.query.all()
    return jsonify([
        {
            'id': p.id,
            'nombre': p.nombre,
            'precio': p.precio,
            'cantidad_litros': p.cantidad_litros
        } for p in productos
    ])

@app.route('/api/productos', methods=['POST'])
def agregar_producto():
    data = request.get_json()
    nuevo = Producto(
        nombre=data['nombre'],
        precio=data['precio'],
        cantidad_litros=data['cantidad_litros']
    )
    db.session.add(nuevo)
    db.session.commit()
    return jsonify({"status": "ok"})

@app.route('/api/productos/<int:id>', methods=['DELETE'])
def eliminar_producto(id):
    producto = Producto.query.get(id)
    if producto:
        db.session.delete(producto)
        db.session.commit()
        return jsonify({"status": "eliminado"})
    return jsonify({"error": "Producto no encontrado"}), 404

@app.route('/api/generar_qr/<int:id>', methods=['GET'])
def generar_qr(id):
    producto = Producto.query.get(id)
    if not producto:
        return jsonify({"error": "Producto no encontrado"}), 404

    access_token = os.getenv("MP_ACCESS_TOKEN")
    if not access_token:
        return jsonify({"error": "Falta MP_ACCESS_TOKEN"}), 500

    url = "https://api.mercadopago.com/checkout/preferences"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }
    body = {
        "items": [{
            "title": producto.nombre,
            "quantity": 1,
            "currency_id": "ARS",
            "unit_price": producto.precio
        }],
        "notification_url": os.getenv("WEBHOOK_URL", "https://example.com/webhook"),
        "external_reference": str(producto.id)
    }

    resp = requests.post(url, json=body, headers=headers)
    if resp.status_code == 201:
        return jsonify({"qr_url": resp.json()["init_point"]})
    else:
        return jsonify({"error": "Error al generar QR", "detalles": resp.text}), 500

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json()
    payment_id = data.get("id")
    producto_id = data.get("producto_id")

    if payment_id and producto_id:
        nuevo = Pago(payment_id=str(payment_id), producto_id=producto_id)
        db.session.add(nuevo)
        db.session.commit()
        return jsonify({"status": "guardado"})

    return jsonify({"status": "error"}), 400

@app.route('/check_payment_pendiente')
def check_payment_pendiente():
    pago = Pago.query.filter_by(dispensado=False).first()
    if pago:
        producto = Producto.query.get(pago.producto_id)
        if producto:
            return jsonify({
                "pago_id": pago.payment_id,
                "producto_id": producto.id,
                "nombre": producto.nombre,
                "cantidad_litros": producto.cantidad_litros
            })
    return jsonify({"status": "sin pagos pendientes"})

@app.route('/marcar_dispensado', methods=['POST'])
def marcar_dispensado():
    data = request.get_json()
    id_pago = data.get("id_pago")
    pago = Pago.query.filter_by(payment_id=id_pago).first()
    if pago:
        pago.dispensado = True
        db.session.commit()
        return jsonify({"status": "marcado"})
    return jsonify({"status": "pago no encontrado"}), 404

# Inicialización de la base de datos
def initialize_database():
    with app.app_context():
        db.create_all()

# Punto de entrada
if __name__ == "__main__":
    initialize_database()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
