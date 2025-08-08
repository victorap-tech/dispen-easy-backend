from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
import qrcode
import io
import base64
import requests

app = Flask(__name__)
CORS(app)

# Configuración de la base de datos
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///productos.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# Modelo de producto
class Producto(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(80), nullable=False)
    precio = db.Column(db.Integer, nullable=False)
    cantidad_litros = db.Column(db.Float, nullable=False)

# Crear la base de datos si no existe
with app.app_context():
    db.create_all()

# Ruta de bienvenida
@app.route('/')
def inicio():
    return 'Backend Dispen-Easy funcionando'

# Obtener todos los productos
@app.route('/api/productos', methods=['GET'])
def obtener_productos():
    productos = Producto.query.all()
    resultado = []
    for p in productos:
        resultado.append({
            'id': p.id,
            'nombre': p.nombre,
            'precio': p.precio,
            'cantidad_litros': p.cantidad_litros
        })
    return jsonify(resultado)

# Agregar producto
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
    return jsonify({'mensaje': 'Producto agregado'}), 200

# Eliminar producto
@app.route('/api/productos/<int:id>', methods=['DELETE'])
def eliminar_producto(id):
    producto = Producto.query.get(id)
    if not producto:
        return jsonify({'error': 'Producto no encontrado'}), 404
    db.session.delete(producto)
    db.session.commit()
    return jsonify({'mensaje': 'Producto eliminado'}), 200

# Generar QR a partir del producto
@app.route('/api/generar_qr/<int:id>', methods=['POST'])
def generar_qr(id):
    producto = Producto.query.get(id)
    if not producto:
        return jsonify({'error': 'Producto no encontrado'}), 404

    # Crear preferencia en MercadoPago
    url = 'https://api.mercadopago.com/checkout/preferences'
    headers = {
        'Content-Type': 'application/json',
        'Authorization': 'Bearer TU_ACCESS_TOKEN'  # ⚠️ Reemplazá con tu token real
    }
    payload = {
        "items": [
            {
                "title": producto.nombre,
                "quantity": 1,
                "unit_price": float(producto.precio)
            }
        ],
        "notification_url": "https://webhook.site/prueba"  # opcional
    }

    response = requests.post(url, headers=headers, json=payload)

    if response.status_code != 201:
        return jsonify({'error': 'No se pudo generar link de pago'}), 500

    link = response.json()['init_point']

    # Generar QR
    qr = qrcode.make(link)
    buffer = io.BytesIO()
    qr.save(buffer, format='PNG')
    qr_base64 = base64.b64encode(buffer.getvalue()).decode('utf-8')

    return jsonify({'qr_base64': qr_base64})

# Punto de entrada
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
