import os
from datetime import datetime
from flask import Flask, jsonify, request
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from sqlalchemy import func
import mercadopago

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
app = Flask(__name__)
CORS(app)

DATABASE_URL = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL") or os.getenv("POSTGRES_URL_NON_POOLING")
if not DATABASE_URL:
    # local fallback (no se usa en Railway)
    DATABASE_URL = "sqlite:///data.db"

# Arreglo típico de Railway: postgres -> postgresql
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

# -----------------------------------------------------------------------------
# Modelo (match exacto con tu tabla 'producto')
# -----------------------------------------------------------------------------
class Producto(db.Model):
    __tablename__ = "producto"

    id         = db.Column(db.Integer, primary_key=True)
    nombre     = db.Column(db.String(100), nullable=False)
    precio     = db.Column(db.Float,       nullable=False)     # precio x presentación (o L)
    cantidad   = db.Column(db.Integer,     nullable=False)     # presentación a vender (ej: 1L, 2L, etc.)
    slot_id    = db.Column(db.Integer,     nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
    habilitado = db.Column(db.Boolean,     nullable=False, default=True)

    def to_dict(self):
        return {
            "id": self.id,
            "nombre": self.nombre,
            "precio": self.precio,
            "cantidad": self.cantidad,
            "slot_id": self.slot_id,
            "habilitado": self.habilitado,
            "created_at": (self.created_at.isoformat() if self.created_at else None),
            "updated_at": (self.updated_at.isoformat() if self.updated_at else None),
        }

# -----------------------------------------------------------------------------
# Salud
# -----------------------------------------------------------------------------
@app.get("/api/health")
def health():
    return jsonify({"ok": True, "service": "dispen-easy", "db": True}), 200

# -----------------------------------------------------------------------------
# CRUD Productos
# -----------------------------------------------------------------------------
@app.get("/api/productos")
def listar_productos():
    q = Producto.query.order_by(Producto.id.asc()).all()
    return jsonify([p.to_dict() for p in q])

@app.post("/api/productos")
def crear_producto():
    data = request.get_json(force=True, silent=True) or {}
    try:
        p = Producto(
            nombre     = str(data.get("nombre", "")).strip(),
            precio     = float(data.get("precio", 0) or 0),
            cantidad   = int(data.get("cantidad", 1) or 1),
            slot_id    = int(data.get("slot_id", 1) or 1),
            habilitado = bool(data.get("habilitado", True)),
        )
        if not p.nombre:
            return jsonify({"error": "nombre requerido"}), 400
        db.session.add(p)
        db.session.commit()
        return jsonify(p.to_dict()), 201
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500

@app.put("/api/productos/<int:pid>")
def actualizar_producto(pid):
    data = request.get_json(force=True, silent=True) or {}
    p = Producto.query.get(pid)
    if not p:
        return jsonify({"error": "no encontrado"}), 404
    try:
        if "nombre"     in data: p.nombre     = str(data["nombre"]).strip()
        if "precio"     in data: p.precio     = float(data["precio"] or 0)
        if "cantidad"   in data: p.cantidad   = int(data["cantidad"] or 1)
        if "slot_id"    in data: p.slot_id    = int(data["slot_id"] or 1)
        if "habilitado" in data: p.habilitado = bool(data["habilitado"])
        p.updated_at = datetime.utcnow()
        db.session.commit()
        return jsonify(p.to_dict())
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500

@app.delete("/api/productos/<int:pid>")
def borrar_producto(pid):
    p = Producto.query.get(pid)
    if not p:
        return jsonify({"error": "no encontrado"}), 404
    try:
        db.session.delete(p)
        db.session.commit()
        return jsonify({"ok": True})
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500

# -----------------------------------------------------------------------------
# MercadoPago: preferencia y link/QR
# -----------------------------------------------------------------------------
@app.post("/api/mp/preferencia/<int:pid>")
def mp_preferencia(pid):
    """Genera preferencia para el producto y devuelve init_point + id preferencia.
       Requiere env MP_ACCESS_TOKEN (token de producción o test)."""
    p = Producto.query.get(pid)
    if not p or not p.habilitado:
        return jsonify({"error": "producto inexistente o no habilitado"}), 404

    access_token = os.getenv("MP_ACCESS_TOKEN")
    if not access_token:
        return jsonify({"error": "MP_ACCESS_TOKEN no configurado"}), 500

    sdk = mercadopago.SDK(access_token)

    # Ítem con precio y cantidad fija = 1 (una venta por vez)
    preference_data = {
        "items": [
            {
                "title": f"{p.nombre} ({p.cantidad}L) - Slot {p.slot_id}",
                "quantity": 1,
                "unit_price": round(float(p.precio), 2),
                "currency_id": "ARS",
            }
        ],
        "metadata": {
            "producto_id": p.id,
            "slot_id": p.slot_id,
            "presentacion_l": p.cantidad,
        },
        "notification_url": os.getenv("MP_WEBHOOK_URL", ""),  # opcional si tenés webhook
        "auto_return": "approved"
    }

    try:
        pref = sdk.preference().create(preference_data)
        body = pref.get("response", {})
        init_point = body.get("init_point")
        pref_id = body.get("id")

        # Link QR “rápido”: muchos usan el init_point directo. Si tenés tu generador de QR en el front,
        # con el init_point alcanza (se muestra como QR Canvas/IMG).
        return jsonify({
            "ok": True,
            "preference_id": pref_id,
            "init_point": init_point
        })
    except Exception as e:
        return jsonify({"error": f"MercadoPago: {e}"}), 500

# -----------------------------------------------------------------------------
# Main local
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    # En Railway no se ejecuta, pero localmente sirve
    with app.app_context():
        # NO crea tablas nuevas en Postgres si ya existen; en Postgres real tu esquema ya está creado.
        db.create_all()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))
