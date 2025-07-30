from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
import os
from datetime import datetime

app = Flask(__name__)
CORS(app)

# Configuración de la base de datos SQLite
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///pagos.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# MODELOS

class Producto(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(100))
    precio = db.Column(db.Float)
    cantidad_ml = db.Column(db.Integer)

class Pago(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    payment_id = db.Column(db.String(50), unique=True)
    producto_id = db.Column(db.Integer, db.ForeignKey('producto.id'))
    aprobado = db.Column(db.Boolean, default=False)
    dispensado = db.Column(db.Boolean, default=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

# RUTAS

@app.route('/')
def index():
    return 'Backend Dispen-Easy funcionando correctamente'

@app.route('/api/productos', methods=['GET'])
def obtener_productos():
    productos = Producto.query.all()
    data = []
    for p in productos:
        data.append({
            'id': p.id,
            'nombre': p.nombre,
            'precio': p.precio,
            'cantidad_ml': p.cantidad_ml
        })
    return jsonify(data)

@app.route('/api/productos', methods=['POST'])
def agregar_producto():
    data = request.get_json()
    nombre = data.get('nombre')
    precio = data.get('precio')
    cantidad_ml = data.get('cantidad_ml')
    nuevo = Producto(nombre=nombre, precio=precio, cantidad_ml=cantidad_ml)
    db.session.add(nuevo)
    db.session.commit()
    return jsonify({'status': 'ok'})

@app.route('/api/productos/<int:id>', methods=['DELETE'])
def eliminar_producto(id):
    producto = Producto.query.get(id)
    if producto:
        db.session.delete(producto)
        db.session.commit()
        return jsonify({'status': 'eliminado'})
    return jsonify({'status': 'no encontrado'}), 404

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json()
    payment_id = str(data.get('data', {}).get('id'))
    producto_id = int(data.get('producto_id'))

    if not payment_id or not producto_id:
        return jsonify({"status": "faltan datos"}), 400

    nuevo_pago = Pago(payment_id=payment_id, producto_id=producto_id)
    db.session.add(nuevo_pago)
    db.session.commit()
    return jsonify({"status": "guardado"})

@app.route('/check_payment_pendiente', methods=['GET'])
def check_pendiente():
    pago = Pago.query.filter_by(aprobado=True, dispensado=False).first()
    if pago:
        producto = Producto.query.get(pago.producto_id)
        return jsonify({
            "id_pago": pago.payment_id,
            "producto_id": producto.id,
            "cantidad_ml": producto.cantidad_ml
        })
    return jsonify({"status": "sin pagos pendientes"}), 404

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

@app.route('/aprobar_pago', methods=['POST'])
def aprobar_pago():
    data = request.get_json()
    id_pago = data.get("id_pago")
    pago = Pago.query.filter_by(payment_id=id_pago).first()
    if pago:
        pago.aprobado = True
        db.session.commit()
        return jsonify({"status": "aprobado"})
    return jsonify({"status": "pago no encontrado"}), 404

# INICIALIZACIÓN

def initialize_database():
    with app.app_context():
        db.drop_all()       # ⚠️ solo temporal: borra todas las tablas
        db.create_all()

if __name__ == '__main__':
    initialize_database()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
