# app.py - Backend Flask funcional
from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
import os

app = Flask(__name__)

CORS(app, resources={r"/api/*": {"origins": ["https://dispen-easy-web-production.up.railway.app", "http://localhost:3000"]}})

app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL")
db = SQLAlchemy(app)

class Producto(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(100))
    precio = db.Column(db.Float)
    cantidad = db.Column(db.Float)

@app.route('/api/productos', methods=['GET'])
def get_productos():
    productos = Producto.query.all()
    return jsonify([{"id": p.id, "nombre": p.nombre, "precio": p.precio, "cantidad": p.cantidad} for p in productos])

@app.route('/api/productos', methods=['POST'])
def add_producto():
    data = request.json
    nuevo = Producto(nombre=data["nombre"], precio=data["precio"], cantidad=data["cantidad"])
    db.session.add(nuevo)
    db.session.commit()
    return jsonify({"message": "Producto agregado"}), 201

@app.route('/api/productos/<int:id>', methods=['DELETE'])
def delete_producto(id):
    prod = Producto.query.get(id)
    if prod:
        db.session.delete(prod)
        db.session.commit()
        return jsonify({"message": "Producto eliminado"})
    return jsonify({"message": "No encontrado"}), 404

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
