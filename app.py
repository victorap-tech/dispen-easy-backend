from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
import os

app = Flask(__name__)
CORS(app)

# Configuración base de datos (usa SQLite por defecto)
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv("DATABASE_URL", "sqlite:///productos.db")
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# Modelo de Producto
class Producto(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    slot_id = db.Column(db.Integer, nullable=False, unique=True)  # salida fija 1-6
    nombre = db.Column(db.String(100), nullable=False)
    precio = db.Column(db.Float, nullable=False, default=0.0)
    cantidad = db.Column(db.Float, nullable=False, default=0.0)  # litros
    habilitado = db.Column(db.Boolean, default=False)

# Inicializar siempre 6 slots
def init_slots():
    if Producto.query.count() == 0:
        for i in range(1, 7):  # 6 productos fijos
            p = Producto(
                slot_id=i,
                nombre=f"Producto {i}",
                precio=0.0,
                cantidad=0.0,
                habilitado=False
            )
            db.session.add(p)
        db.session.commit()

# ------------------ Rutas ------------------

@app.route("/")
def home():
    return "✅ Backend Dispen-Easy activo"

# Obtener todos los productos
@app.route("/api/productos", methods=["GET"])
def get_productos():
    productos = Producto.query.all()
    data = []
    for p in productos:
        data.append({
            "id": p.id,
            "slot_id": p.slot_id,
            "nombre": p.nombre,
            "precio": p.precio,
            "cantidad": p.cantidad,
            "habilitado": p.habilitado
        })
    return jsonify(data)

# Editar un producto (por slot fijo)
@app.route("/api/productos/<int:slot_id>", methods=["PUT"])
def update_producto(slot_id):
    producto = Producto.query.filter_by(slot_id=slot_id).first()
    if not producto:
        return jsonify({"error": "Producto no encontrado"}), 404

    data = request.json
    producto.nombre = data.get("nombre", producto.nombre)
    producto.precio = data.get("precio", producto.precio)
    producto.cantidad = data.get("cantidad", producto.cantidad)
    producto.habilitado = data.get("habilitado", producto.habilitado)

    db.session.commit()
    return jsonify({"message": "Producto actualizado"})

# Webhook de MercadoPago
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    print("🔔 Webhook recibido:", data)
    # aquí procesás el pago real
    return jsonify({"status": "ok"}), 200

# ------------------ Main ------------------

if __name__ == "__main__":
    with app.app_context():
        db.create_all()
        init_slots()  # siempre asegura los 6 slots fijos
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
