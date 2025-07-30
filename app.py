from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
import os

app = Flask(__name__)
CORS(app)

# Configurar base de datos SQLite
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///productos.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# Modelo de Producto
class Producto(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(80), nullable=False)
    precio = db.Column(db.Float, nullable=False)
    cantidad_ml = db.Column(db.Integer, nullable=False, default=500)

# Crear tablas antes de la primera petición
@app.before_first_request
def crear_tablas():
    db.create_all()

# Obtener todos los productos
@app.route('/api/productos', methods=['GET'])
def obtener_productos():
    productos = Producto.query.all()
    return jsonify([{
        'id': p.id,
        'nombre': p.nombre,
        'precio': p.precio,
        'cantidad_ml': p.cantidad_ml
    } for p in productos])

# Agregar producto
@app.route('/api/productos', methods=['POST'])
def agregar_producto():
    data = request.get_json()
    nuevo_producto = Producto(
        nombre=data['nombre'],
        precio=data['precio'],
        cantidad_ml=data.get('cantidad_ml', 500)
    )
    db.session.add(nuevo_producto)
    db.session.commit()
    return jsonify({'mensaje': 'Producto agregado'}), 201

# Eliminar producto
@app.route('/api/productos/<int:producto_id>', methods=['DELETE'])
def eliminar_producto(producto_id):
    producto = Producto.query.get_or_404(producto_id)
    db.session.delete(producto)
    db.session.commit()
    return jsonify({'mensaje': 'Producto eliminado'})

# Puerto y ejecución
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
