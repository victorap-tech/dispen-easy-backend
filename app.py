from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
import os
import paho.mqtt.client as mqtt

app = Flask(__name__)
CORS(app)

# Configuración base de datos
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///pagos.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# Modelo
class Producto(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(100), nullable=False)
    precio = db.Column(db.Float, nullable=False)
    cantidad_ml = db.Column(db.Integer, nullable=False)

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
    mqtt_client.connect(mqtt_broker, mqtt_port, 60)

# Endpoints

@app.route('/')
def index():
    return 'Backend Dispen-Easy funcionando correctamente'

@app.route('/api/productos', methods=['GET'])
def listar_productos():
    productos = Producto.query.all()
    resultado = []
    for p in productos:
        resultado.append({
            'id': p.id,
            'nombre': p.nombre,
            'precio': p.precio,
            'cantidad_ml': p.cantidad_ml
        })
    return jsonify(resultado)

@app.route('/api/productos', methods=['POST'])
def agregar_producto():
    data = request.get_json()
    nuevo = Producto(
        nombre=data['nombre'],
        precio=data['precio'],
        cantidad_ml=data['cantidad_ml']
    )
    db.session.add(nuevo)
    db.session.commit()
    return jsonify({"status": "ok"})

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
                "cantidad_ml": producto.cantidad_ml
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

# Inicializar base de datos al iniciar
def initialize_database():
    with app.app_context():
        db.create_all()

if __name__ == "__main__":
    initialize_database()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

