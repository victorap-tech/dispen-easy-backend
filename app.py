from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
import os
import datetime
import paho.mqtt.client as mqtt
import requests
import json
from decimal import Decimal

# --- Configuración de la Aplicación Flask ---
app = Flask(__name__)
CORS(app, supports_credentials=True)  # Habilita CORS para frontend/ESP32

# --- Configuración de la Base de Datos ---
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'sqlite:///pagos.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# --- Modelo Producto ---
class Producto(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(50))
    precio = db.Column(db.Numeric(10, 2))
    cantidad = db.Column(db.Integer)

# --- Ruta base para verificación ---
@app.route("/", methods=["GET"])
def index():
    return "✅ Backend Dispen-Easy funcionando"

# --- Obtener productos ---
@app.route("/api/productos", methods=["GET", "OPTIONS"])
def get_productos():
    productos = Producto.query.all()
    return jsonify([
        {"id": p.id, "nombre": p.nombre, "precio": float(p.precio), "cantidad": p.cantidad}
        for p in productos
    ])

# --- Agregar producto ---
@app.route("/api/productos", methods=["POST", "OPTIONS"])
def agregar_producto():
    data = request.get_json()
    nuevo = Producto(
        nombre=data["nombre"],
        precio=Decimal(data["precio"]),
        cantidad=int(data["cantidad"])
    )
    db.session.add(nuevo)
    db.session.commit()
    return jsonify({"mensaje": "Producto agregado"}), 201

# --- Eliminar producto ---
@app.route("/api/productos/<int:id>", methods=["DELETE", "OPTIONS"])
def eliminar_producto(id):
    producto = Producto.query.get_or_404(id)
    db.session.delete(producto)
    db.session.commit()
    return jsonify({"mensaje": "Producto eliminado"})

# --- Generar QR (simulado para ejemplo) ---

@app.route("/api/generar_qr/<int:id>", methods=["POST", "OPTIONS"])
def generar_qr(id):
    producto = Producto.query.get_or_404(id)

    access_token = os.getenv("MP_ACCESS_TOKEN")
    if not access_token:
        return jsonify({"error": "Falta el token de MercadoPago"}), 500

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }

    payload = {
        "items": [
            {
                "title": producto.nombre,
                "quantity": 1,
                "unit_price": float(producto.precio),
                "currency_id": "ARS"
            }
        ],
        "back_urls": {
            "success": "https://dispen-easy-web-production.up.railway.app",
            "failure": "https://dispen-easy-web-production.up.railway.app",
            "pending": "https://dispen-easy-web-production.up.railway.app"
        },
        "auto_return": "approved",
        "external_reference": str(producto.id)
    }

    try:
        res = requests.post(
            "https://api.mercadopago.com/checkout/preferences",
            headers=headers,
            json=payload
        )
        res.raise_for_status()
        data = res.json()
        return jsonify({"url": data["init_point"]})
    except requests.exceptions.RequestException as e:
        return jsonify({"error": str(e)}), 500

# --- Inicializar base de datos si no existe ---
with app.app_context():
    db.create_all()

# --- Iniciar servidor si se ejecuta directamente ---
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=True, host="0.0.0.0", port=port)
