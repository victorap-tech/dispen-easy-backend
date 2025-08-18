import os
from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
import paho.mqtt.client as mqtt

# -------------------------------
# Configuración Flask
# -------------------------------
app = Flask(__name__)
CORS(app)

app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///pagos.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

# -------------------------------
# Modelo Producto
# -------------------------------
class Producto(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    slot_id = db.Column(db.Integer, unique=True, nullable=False)
    nombre = db.Column(db.String(100), default="")
    precio = db.Column(db.Float, default=0.0)
    cantidad = db.Column(db.Float, default=0.0)
    habilitado = db.Column(db.Boolean, default=False)

    def to_dict(self):
        return {
            "id": self.id,
            "slot_id": self.slot_id,
            "nombre": self.nombre,
            "precio": self.precio,
            "cantidad": self.cantidad,
            "habilitado": self.habilitado,
        }

# -------------------------------
# Crear DB + 6 filas iniciales
# -------------------------------
with app.app_context():
    db.create_all()

    # Si la tabla está vacía, insertar 6 productos por defecto
    if Producto.query.count() == 0:
        for i in range(1, 7):
            nuevo = Producto(
                slot_id=i,
                nombre=f"Producto {i}",
                precio=0.0,
                cantidad=0,
                habilitado=False
            )
            db.session.add(nuevo)
        db.session.commit()

# -------------------------------
# Endpoints CRUD
# -------------------------------

@app.route("/api/productos", methods=["GET"])
def get_productos():
    productos = Producto.query.order_by(Producto.slot_id).all()
    return jsonify([p.to_dict() for p in productos])

@app.route("/api/productos/<int:slot_id>", methods=["PUT"])
def update_producto(slot_id):
    data = request.json
    producto = Producto.query.filter_by(slot_id=slot_id).first()
    if not producto:
        return jsonify({"error": "Producto no encontrado"}), 404

    producto.nombre = data.get("nombre", producto.nombre)
    producto.precio = data.get("precio", producto.precio)
    producto.cantidad = data.get("cantidad", producto.cantidad)
    producto.habilitado = data.get("habilitado", producto.habilitado)

    db.session.commit()
    return jsonify(producto.to_dict())

@app.route("/api/productos/<int:slot_id>", methods=["DELETE"])
def delete_producto(slot_id):
    producto = Producto.query.filter_by(slot_id=slot_id).first()
    if not producto:
        return jsonify({"error": "Producto no encontrado"}), 404

    db.session.delete(producto)
    db.session.commit()
    return jsonify({"success": True})

# -------------------------------
# MQTT Config
# -------------------------------
broker = os.getenv("MQTT_BROKER", "broker.hivemq.com")
port = int(os.getenv("MQTT_PORT", 1883))
mqtt_client = mqtt.Client()

def on_connect(client, userdata, flags, rc):
    print("Conectado a MQTT con código:", rc)

mqtt_client.on_connect = on_connect
mqtt_client.connect(broker, port, 60)

# -------------------------------
# MercadoPago Webhook
# -------------------------------
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    print("Webhook recibido:", data)
    # 👇 acá podés validar pago y luego enviar al ESP32 por MQTT
    # ejemplo:
    # mqtt_client.publish("dispen-easy/salida1", "ACTIVAR")
    return jsonify({"status": "ok"})

# -------------------------------
# Run
# -------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
