from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
import requests
import qrcode
import io
import base64

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///pagos.db'
db = SQLAlchemy(app)

# MODELO DE BASE DE DATOS
class Pago(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    id_pago = db.Column(db.String(120), unique=True, nullable=False)
    estado = db.Column(db.String(80), nullable=False)
    producto = db.Column(db.String(120), nullable=True)
    dispensado = db.Column(db.Boolean, default=False)

# CREAR TABLAS
with app.app_context():
    db.create_all()

# ENDPOINT PARA CREAR PRODUCTO Y GENERAR QR
@app.route('/api/generar_qr/<int:id>', methods=['GET'])
def generar_qr(id):
    producto = Producto.query.get(id)
    if not producto:
        return jsonify({'error': 'Producto no encontrado'}), 404

    url = 'https://api.mercadopago.com/checkout/preferences'
    headers = {
        'Content-Type': 'application/json',
        'Authorization': 'Bearer TU_ACCESS_TOKEN'  # Reemplazá por tu token real
    }
    payload = {
        "items": [
            {
                "title": producto.nombre,
                "quantity": 1,
                "unit_price": float(producto.precio)
            }
        ],
        "notification_url": "https://web-production-xxxx.up.railway.app/webhook"  # Reemplazá por tu dominio Railway
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

# MODELO DE PRODUCTO
class Producto(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(100), nullable=False)
    precio = db.Column(db.Float, nullable=False)

# ENDPOINT PARA AGREGAR PRODUCTO
@app.route('/api/productos', methods=['POST'])
def agregar_producto():
    data = request.json
    nuevo_producto = Producto(nombre=data['nombre'], precio=data['precio'])
    db.session.add(nuevo_producto)
    db.session.commit()
    return jsonify({'mensaje': 'Producto agregado correctamente'})

# ENDPOINT PARA ELIMINAR PRODUCTO
@app.route('/api/productos/<int:id>', methods=['DELETE'])
def eliminar_producto(id):
    producto = Producto.query.get(id)
    if not producto:
        return jsonify({'error': 'Producto no encontrado'}), 404
    db.session.delete(producto)
    db.session.commit()
    return jsonify({'mensaje': 'Producto eliminado'})

# ENDPOINT PARA LISTAR PRODUCTOS
@app.route('/api/productos', methods=['GET'])
def listar_productos():
    productos = Producto.query.all()
    resultado = [{'id': p.id, 'nombre': p.nombre, 'precio': p.precio} for p in productos]
    return jsonify(resultado)

# WEBHOOK DE MERCADOPAGO
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json
    print("⚡ Webhook recibido:", data)

    id_pago = data.get('data', {}).get('id')
    if id_pago:
        nuevo_pago = Pago(id_pago=id_pago, estado='pendiente', dispensado=False)
        db.session.add(nuevo_pago)
        db.session.commit()

    return '', 200

# CONSULTA DE PAGOS PENDIENTES
@app.route('/check_payment_pendiente', methods=['GET'])
def check_pendiente():
    pago = Pago.query.filter_by(estado='pendiente', dispensado=False).first()
    if pago:
        return jsonify({'id_pago': pago.id_pago})
    else:
        return jsonify({'mensaje': 'No hay pagos pendientes'}), 204

# MARCAR COMO DISPENSADO
@app.route('/marcar_dispensado', methods=['POST'])
def marcar_dispensado():
    data = request.json
    id_pago = data.get('id_pago')
    pago = Pago.query.filter_by(id_pago=id_pago).first()
    if pago:
        pago.estado = 'aprobado'
        pago.dispensado = True
        db.session.commit()
        return jsonify({'mensaje': 'Pago marcado como dispensado'})
    else:
        return jsonify({'error': 'Pago no encontrado'}), 404

# PUNTO DE ENTRADA PARA RAILWAY
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
