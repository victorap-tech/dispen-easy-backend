from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
import os
import json
import paho.mqtt.client as mqtt

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///pagos.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# ---------------- MODELOS ----------------

class Producto(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(100))
    precio = db.Column(db.Float)
    cantidad_ml = db.Column(db.Integer)

class Pago(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    payment_id = db.Column(db.String(50), unique=True)
    estado = db.Column(db.String(20))
    producto_id = db.Column(db.Integer)
    dispensado = db.Column(db.Boolean, default=False)

# ---------------- MQTT ----------------

mqtt_client = mqtt.Client()
mqtt_broker = os.getenv("MQTT_BROKER", "c9b4a2b821ec4e6a8a9c53cb11820b83.s2.eu.hivemq.cloud")
mqtt_port = int(os.getenv("MQTT_PORT", 8883))
mqtt_user = os.getenv("MQTT_USER", "Victor")
mqtt_pass = os.getenv("MQTT_PASS", "Dispeneasy2025")

if mqtt_user and mqtt_pass:
    mqtt_client.username_pw_set(mqtt_user, mqtt_pass)
    mqtt_client.tls_set()
    mqtt_client.connect(mqtt_broker, mqtt_port, 60)

# ---------------- API ----------------

@app.route("/api/productos", methods=["GET"])
def get_productos():
    productos = Producto.query.all()
    return jsonify([{
        "id": p.id,
        "nombre": p.nombre,
        "precio": p.precio,
        "cantidad_ml": p.cantidad_ml
    } for p in productos])

@app.route("/api/productos", methods=["POST"])
def agregar_producto():
    data = request.get_json()
    nuevo = Producto(
        nombre=data["nombre"],
        precio=data["precio"],
        cantidad_ml=data["cantidad_ml"]
    )
    db.session.add(nuevo)
    db.session.commit()
    return jsonify({"mensaje": "Producto agregado"}), 201

@app.route("/api/productos/<int:id>", methods=["DELETE"])
def eliminar_producto(id):
    producto = Producto.query.get(id)
    if producto:
        db.session.delete(producto)
        db.session.commit()
        return jsonify({"mensaje": "Producto eliminado"})
    return jsonify({"error": "Producto no encontrado"}), 404

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    payment_id = data.get("data", {}).get("id")

    if not payment_id:
        return jsonify({"status": "no id"}), 400

    nuevo_pago = Pago(payment_id=str(payment_id), estado="pendiente", producto_id=None)
    db.session.add(nuevo_pago)
    db.session.commit()
    return jsonify({"status": "ok"})

@app.route("/check_payment", methods=["GET"])
def check_payment():
    id_pago = request.args.get("id_pago")
    pago = Pago.query.filter_by(payment_id=id_pago).first()

    if pago:
        return jsonify({
            "estado": pago.estado,
            "producto_id": pago.producto_id
        })
    return jsonify({"estado": "no encontrado"})

@app.route("/check_payment_pendiente", methods=["GET"])
def check_payment_pendiente():
    pago = Pago.query.filter_by(estado="approved", dispensado=False).first()
    if pago:
        producto = Producto.query.get(pago.producto_id)
        if producto:
            mqtt_client.publish("dispensador/comando", json.dumps({
                "id_pago": pago.payment_id,
                "comando": "DISPENSAR",
                "producto_id": producto.id,
                "cantidad_ml": producto.cantidad_ml
            }))
            pago.dispensado = True
            db.session.commit()
            return jsonify({"status": "dispensado"})
    return jsonify({"status": "sin pagos"})

@app.route("/marcar_dispensado", methods=["POST"])
def marcar_dispensado():
    data = request.get_json()
    id_pago = data.get("id_pago")
    pago = Pago.query.filter_by(payment_id=id_pago).first()
    if pago:
        pago.dispensado = True
        db.session.commit()
        return jsonify({"status": "marcado"})
    return jsonify({"status": "pago no encontrado"}), 404

# ---------------- INICIO ----------------

def initialize_database():
    with app.app_context():
        db.create_all()

if __name__ == "__main__":
    initialize_database()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
