from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
import os
import json
import paho.mqtt.client as mqtt

app = Flask(__name__)
CORS(app)

# Configuración base de datos
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///pagos.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# Modelo de Producto
class Producto(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(100))
    precio = db.Column(db.Float)
    cantidad_ml = db.Column(db.Integer)

# Modelo de Pago
class Pago(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    payment_id = db.Column(db.String(100), unique=True)
    estado = db.Column(db.String(20))
    producto_id = db.Column(db.Integer)
    dispensado = db.Column(db.Boolean, default=False)

# MQTT
mqtt_client = mqtt.Client()
mqtt_broker = os.getenv("MQTT_BROKER", "c9b4a2b821ec4e87b10ed8e0ace8e4ee.s1.eu.hivemq.cloud")
mqtt_port = int(os.getenv("MQTT_PORT", 8883))
mqtt_user = os.getenv("Victor")
mqtt_pass = os.getenv("Dispeneasy2025")
if mqtt_user and mqtt_pass:
    mqtt_client.username_pw_set(mqtt_user, mqtt_pass)
mqtt_client.connect(mqtt_broker, mqtt_port, 60)

# Crear base de datos
@app.before_first_request
def crear_tablas():
    db.create_all()

# Agregar producto
@app.route('/api/productos', methods=['POST'])
def agregar_producto():
    data = request.get_json()
    nuevo = Producto(nombre=data['nombre'], precio=data['precio'], cantidad_ml=data.get('cantidad_ml', 500))
    db.session.add(nuevo)
    db.session.commit()
    return jsonify({'mensaje': 'Producto agregado'}), 201

# Obtener productos
@app.route('/api/productos', methods=['GET'])
def obtener_productos():
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

# Eliminar producto
@app.route('/api/productos/<int:id>', methods=['DELETE'])
def eliminar_producto(id):
    producto = Producto.query.get(id)
    if producto:
        db.session.delete(producto)
        db.session.commit()
        return jsonify({'mensaje': 'Producto eliminado'})
    return jsonify({'mensaje': 'Producto no encontrado'}), 404

# Webhook MercadoPago
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json()
    payment_id = str(data.get("data", {}).get("id"))
    if not payment_id:
        return jsonify({"status": "missing id"}), 400
    existente = Pago.query.filter_by(payment_id=payment_id).first()
    if not existente:
        nuevo_pago = Pago(payment_id=payment_id, estado="pendiente", producto_id=0)
        db.session.add(nuevo_pago)
        db.session.commit()
    return jsonify({"status": "ok"}), 200

# Verificar pago por ID
@app.route('/check_payment', methods=['GET'])
def check_payment():
    payment_id = request.args.get('id_pago')
    pago = Pago.query.filter_by(payment_id=payment_id).first()
    if pago:
        return jsonify({"estado": pago.estado, "producto_id": pago.producto_id})
    return jsonify({"estado": "no_encontrado"})

# Verificar si hay pagos pendientes
@app.route('/check_payment_pendiente', methods=['GET'])
def check_pendiente():
    pago = Pago.query.filter_by(estado="approved", dispensado=False).first()
    if pago:
        producto = Producto.query.get(pago.producto_id)
        return jsonify({
            "id_pago": pago.payment_id,
            "producto_id": producto.id,
            "cantidad_ml": producto.cantidad_ml
        })
    return jsonify({"status": "sin_pagos"})

# Marcar como dispensado
@app.route('/marcar_dispensado', methods=['POST'])
def marcar_dispensado():
    data = request.get_json()
    payment_id = data.get("id_pago")
    pago = Pago.query.filter_by(payment_id=payment_id).first()
    if pago:
        pago.dispensado = True
        db.session.commit()
        return jsonify({"status": "ok"})
    return jsonify({"status": "no_encontrado"})

if __name__ == '__main__':
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
