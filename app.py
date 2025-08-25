import os
from flask import Flask, jsonify, request
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from sqlalchemy import func

# -------------------------
# Config básica
# -------------------------
app = Flask(__name__)
CORS(app)

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///data.db")
# Railway en ocasiones entrega "postgres://"
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

# -------------------------
# Modelo
# -------------------------
class Producto(db.Model):
    __tablename__ = "producto"

    id         = db.Column(db.Integer, primary_key=True)
    nombre     = db.Column(db.String(100), nullable=False)
    precio     = db.Column(db.Float,       nullable=False)
    cantidad   = db.Column(db.Integer,     nullable=False)
    slot_id    = db.Column(db.Integer,     nullable=False)
    habilitado = db.Column(db.Boolean,     nullable=False, default=False)

    created_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    def to_dict(self):
        return {
            "id": self.id,
            "nombre": self.nombre,
            "precio": self.precio,
            "cantidad": self.cantidad,
            "slot_id": self.slot_id,
            "habilitado": self.habilitado,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

# -------------------------
# Rutas
# -------------------------
@app.get("/api/health")
def health():
    return jsonify({"ok": True})

@app.get("/api/productos")
def productos_list():
    prods = Producto.query.order_by(Producto.id.asc()).all()
    return jsonify([p.to_dict() for p in prods])

@app.post("/api/productos")
def productos_create():
    data = request.get_json(force=True) or {}

    try:
        nombre   = str(data.get("nombre", "")).strip()
        precio   = float(data.get("precio", 0))
        cantidad = int(data.get("cantidad", 0))
        slot_id  = int(data.get("slot_id", 0))
        habilitado = bool(data.get("habilitado", False))
    except (TypeError, ValueError):
        return jsonify({"error": "Datos inválidos"}), 400

    if not nombre:
        return jsonify({"error": "El nombre es obligatorio"}), 400
    if precio < 0 or cantidad < 0 or slot_id <= 0:
        return jsonify({"error": "precio/cantidad/slot_id inválidos"}), 400

    p = Producto(
        nombre=nombre,
        precio=precio,
        cantidad=cantidad,
        slot_id=slot_id,
        habilitado=habilitado,
    )
    db.session.add(p)
    db.session.commit()
    return jsonify(p.to_dict()), 201

@app.patch("/api/productos/<int:pid>")
def productos_update(pid):
    p = Producto.query.get_or_404(pid)
    data = request.get_json(force=True) or {}

    if "nombre" in data:
        nombre = str(data["nombre"]).strip()
        if not nombre:
            return jsonify({"error": "nombre no puede ser vacío"}), 400
        p.nombre = nombre

    if "precio" in data:
        try:
            p.precio = float(data["precio"])
        except (TypeError, ValueError):
            return jsonify({"error": "precio inválido"}), 400

    if "cantidad" in data:
        try:
            cant = int(data["cantidad"])
            if cant < 0: raise ValueError()
            p.cantidad = cant
        except (TypeError, ValueError):
            return jsonify({"error": "cantidad inválida"}), 400

    if "slot_id" in data:
        try:
            sid = int(data["slot_id"])
            if sid <= 0: raise ValueError()
            p.slot_id = sid
        except (TypeError, ValueError):
            return jsonify({"error": "slot_id inválido"}), 400

    if "habilitado" in data:
        p.habilitado = bool(data["habilitado"])

    db.session.commit()
    return jsonify(p.to_dict())

@app.post("/api/productos/<int:pid>/toggle")
def productos_toggle(pid):
    p = Producto.query.get_or_404(pid)
    p.habilitado = not p.habilitado
    db.session.commit()
    return jsonify(p.to_dict())

@app.delete("/api/productos/<int:pid>")
def productos_delete(pid):
    p = Producto.query.get_or_404(pid)
    db.session.delete(p)
    db.session.commit()
    return jsonify({"ok": True})

# -------------------------
# Inicialización sin before_first_request
# -------------------------
def init_db():
    with app.app_context():
        db.create_all()

init_db()

if __name__ == "__main__":
    # Útil para correr local
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
