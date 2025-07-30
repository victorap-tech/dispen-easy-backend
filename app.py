from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
import os
import paho.mqtt.client as mqtt
import mercadopago

app = Flask(__name__)
CORS(app)

# Configuración base de datos
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///pagos.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# --- MODELOS ---
class Producto(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(100), nullable=False)
    precio = db.Column(db.Float, nullable=False)
    cantidad_ml = db.Column(db.Integer, nullable=False)

class Pago(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    payment_id = db.Column(db.String(100), unique=True)
    producto_id = db.Column(db.Integer, db.ForeignKey('producto.id'))
    estado = db.Column(db.String(50), default="pendiente")
    dispensado = db.Column(db.Boolean, default=False)

# --- MQTT ---
mqtt_client = mqtt.Client()
mqtt_broker = os.getenv("MQTT_BROKER", "c9b4a2b821ec4e64b81628b63d4f452c.s1.eu.hivemq.cloud")
mqtt_port = int(os.getenv("MQTT_PORT", 8883))
mqtt_user = os.getenv("MQTT_USER", "Victor")
mqtt_pass = os.getenv("MQTT_PASS", "Dispeneasy2025")

mqtt_client.username_pw_set(mqtt_user, mqtt_pass)
mqtt_client.connect(mqtt_broker, mqtt_port, 60)
mqtt_client.loop_start()

# --- ENDPOINTS ---

@app.route('/')
def home():
    return '✅ Backend Dispen-Easy con MQTT funcionando correctamente'

@app.route('/api/productos', methods=['GET'])
def listar_productos():
    productos = Producto.query.all()
    return jsonify([
        {
            'id': p.id,
            'nombre': p.nombre,
            'precio': p.precio,
            'cantidad_ml': p.cantidad_ml
        } for p in productos
    ])

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
    return jsonify({"status": "ok", "id": nuevo.id})

@app.route('/api/productos/<int:id>', methods=['DELETE'])
def eliminar_producto(id):
    producto = Producto.query.get(id)
    if producto:
        db.session.delete(producto)
        db.session.commit()
        return jsonify({"status": "eliminado"})
    return jsonify({"error": "producto no encontrado"}), 404

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json()
    payment_id = str(data.get("id"))
    producto_id = data.get("producto_id")

    if payment_id and producto_id:
        ya_existe = Pago.query.filter_by(payment_id=payment_id).first()
        if ya_existe:
            return jsonify({"status": "ya registrado"})

        nuevo_pago = Pago(payment_id=payment_id, producto_id=producto_id, estado="aprobado")
        db.session.add(nuevo_pago)
        db.session.commit()

        # Publicar por MQTT al dispensador
        producto = Producto.query.get(producto_id)
        if producto:
            mensaje = f'DISPENSAR:{producto.id}:{producto.cantidad_ml}'
            mqtt_client.publish("dispen-easy/comando", mensaje)
            print(f"[MQTT] Enviado: {mensaje}")
        
        return jsonify({"status": "guardado"})
    return jsonify({"status": "error"}), 400

@app.route('/check_payment_pendiente')
def check_payment_pendiente():
    pago = Pago.query.filter_by(estado="aprobado", dispensado=False).first()
    if pago:
        producto = Producto.query.get(pago.producto_id)
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
    id_pago = str(data.get("id_pago"))
    pago = Pago.query.filter_by(payment_id=id_pago).first()
    if pago:
        pago.dispensado = True
        db.session.commit()
        return jsonify({"status": "marcado"})
    return jsonify({"status": "pago no encontrado"}), 404

# --- Inicializar DB ---
def initialize_database():
    with app.app_context():
        db.create_all()
        

# Configurar MercadoPago con tu token de acceso (de producción o test)
sdk = mercadopago.SDK(os.getenv("MP_ACCESS_TOKEN", "TU_ACCESS_TOKEN_AQUI"))

@app.route('/api/generar_qr/<int:id_producto>', methods=['POST'])
def generar_qr(id_producto):
    producto = Producto.query.get(id_producto)
    if not producto:
        return jsonify({"status": "producto no encontrado"}), 404

    preference_data = {
        "items": [
            {
                "title": producto.nombre,
                "quantity": 1,
                "unit_price": float(producto.precio),
            }
        ],
        "back_urls": {
            "success": "https://www.google.com",
            "failure": "https://www.google.com",
            "pending": "https://www.google.com"
        },
        "auto_return": "approved",
        "external_reference": str(producto.id)
    }

    preference_response = sdk.preference().create(preference_data)
    init_point = preference_response["response"]["init_point"]

    return jsonify({"url": init_point})

if __name__ == '__main__':
    initialize_database()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
