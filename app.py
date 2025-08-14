# app.py
# ============================================================
# Dispen-Easy backend (Flask + SQLAlchemy) - versión completa
# con migración robusta y endpoints de administración.
# ============================================================

import os
import base64
import io
from datetime import datetime

from flask import Flask, jsonify, request, Blueprint
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, text
from sqlalchemy.sql import func

# -----------------------------------
# Configuración base del server
# -----------------------------------
app = Flask(__name__)

# Base de datos (Railway provee DATABASE_URL para Postgres)
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///dispen_easy.db")
app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

# CORS para todo lo que cuelgue de /api/
CORS(app, resources={r"/api/*": {"origins": "*"}})

# -----------------------------------
# Modelos
# -----------------------------------
class Producto(db.Model):
    __tablename__ = "producto"
    id        = db.Column(db.Integer, primary_key=True)
    nombre    = db.Column(db.String(120), nullable=False)
    precio    = db.Column(db.Float, nullable=False)        # en ARS
    cantidad  = db.Column(db.Integer, nullable=False, default=0)  # litros
    slot_id   = db.Column(db.Integer, nullable=False, default=1)  # salida física 1..6
    created_at = db.Column(db.DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), onupdate=func.now())

    def to_dict(self):
        return {
            "id": self.id,
            "nombre": self.nombre,
            "precio": self.precio,
            "cantidad": self.cantidad,
            "slot_id": self.slot_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

# Crear tablas si no existen (estructura base)
with app.app_context():
    db.create_all()

# -----------------------------------
# MIGRACIÓN ROBUSTA (admin)
# -----------------------------------
migrate_bp = Blueprint("migrate_bp", __name__)

def _exec(engine, sql):
    with engine.begin() as conn:
        conn.execute(text(sql))

@migrate_bp.get("/migrate")
def admin_migrate():
    """
    Asegura que la tabla 'producto' tenga:
      - slot_id (INT NOT NULL DEFAULT 1)
      - created_at (TIMESTAMPTZ/DATETIME NOT NULL DEFAULT NOW/CURRENT_TIMESTAMP)
      - updated_at (nullable)
      - índice idx_producto_slot_id
    No falla si ya existen.
    """
    token = request.args.get("token")
    expected = os.getenv("MIGRATION_TOKEN")
    if not expected or token != expected:
        return jsonify({"ok": False, "detail": "forbidden"}), 403

    engine = db.engine
    insp = inspect(engine)
    actions = []

    try:
        tables = insp.get_table_names()
        if "producto" not in tables:
            return jsonify({"ok": False, "detail": "tabla 'producto' no existe"}), 400

        cols = {c["name"] for c in insp.get_columns("producto")}
        dialect = engine.url.get_dialect().name.lower()

        # slot_id
        if "slot_id" not in cols:
            _exec(engine, "ALTER TABLE producto ADD COLUMN slot_id INTEGER NOT NULL DEFAULT 1;")
            actions.append("ADD slot_id")
        else:
            actions.append("slot_id ya existe")

        # created_at
        if "created_at" not in cols:
            if dialect in ("postgresql", "postgres"):
                _exec(engine, "ALTER TABLE producto ADD COLUMN created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();")
            else:  # sqlite u otros
                _exec(engine, "ALTER TABLE producto ADD COLUMN created_at DATETIME NOT NULL DEFAULT (CURRENT_TIMESTAMP);")
            actions.append("ADD created_at")
        else:
            actions.append("created_at ya existe")

        # updated_at (opcional)
        if "updated_at" not in cols:
            if dialect in ("postgresql", "postgres"):
                _exec(engine, "ALTER TABLE producto ADD COLUMN updated_at TIMESTAMPTZ;")
            else:
                _exec(engine, "ALTER TABLE producto ADD COLUMN updated_at DATETIME;")
            actions.append("ADD updated_at")
        else:
            actions.append("updated_at ya existe")

        # índice por slot_id
        try:
            _exec(engine, "CREATE INDEX IF NOT EXISTS idx_producto_slot_id ON producto (slot_id);")
            actions.append("INDEX slot_id OK")
        except Exception as ie:
            actions.append(f"INDEX SKIP/ERR: {ie}")

        return jsonify({"ok": True, "dialect": dialect, "actions": actions})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "actions": actions}), 500

@migrate_bp.get("/dbinfo")
def admin_dbinfo():
    """Devuelve dialecto, tablas y columnas (serializable JSON)."""
    token = request.args.get("token")
    expected = os.getenv("MIGRATION_TOKEN")
    if not expected or token != expected:
        return jsonify({"ok": False, "detail": "forbidden"}), 403

    engine = db.engine
    insp = inspect(engine)
    tables = insp.get_table_names()
    data = {"ok": True, "dialect": engine.url.get_dialect().name, "tables": tables, "columns": {}}

    for t in tables:
        cols = insp.get_columns(t)
        safe_cols = []
        for c in cols:
            safe_cols.append({
                "name": c.get("name"),
                "type": str(c.get("type")),
                "nullable": bool(c.get("nullable")),
                "default": str(c.get("default")),
            })
        data["columns"][t] = safe_cols
    return jsonify(data)

app.register_blueprint(migrate_bp, url_prefix="/admin")

# -----------------------------------
# ENDPOINTS API
# -----------------------------------

@app.get("/health")
def health():
    return {"ok": True, "time": datetime.utcnow().isoformat()}, 200

@app.get("/api/productos")
def listar_productos():
    """Lista productos. Soporta filtro opcional por ?slot_id=."""
    try:
        slot_id = request.args.get("slot_id", type=int)
        q = Producto.query
        if slot_id:
            q = q.filter_by(slot_id=slot_id)
        productos = q.order_by(Producto.id.asc()).all()
        return jsonify([p.to_dict() for p in productos]), 200
    except Exception as e:
        app.logger.exception(e)
        return jsonify({"detail": str(e)}), 500

@app.post("/api/productos")
def crear_producto():
    try:
        data = request.get_json(force=True, silent=True) or {}
        nombre = (data.get("nombre") or "").strip()
        precio = data.get("precio")
        cantidad = data.get("cantidad")
        slot_id = data.get("slot_id")

        if not nombre or precio is None or cantidad is None or slot_id is None:
            return jsonify({"detail": "Faltan campos: nombre, precio, cantidad, slot_id"}), 400

        p = Producto(
            nombre=nombre,
            precio=float(precio),
            cantidad=int(cantidad),
            slot_id=int(slot_id),
        )
        db.session.add(p)
        db.session.commit()
        return jsonify(p.to_dict()), 201
    except Exception as e:
        db.session.rollback()
        app.logger.exception(e)
        return jsonify({"detail": str(e)}), 500

@app.delete("/api/productos/<int:pid>")
def eliminar_producto(pid):
    try:
        p = Producto.query.get(pid)
        if not p:
            return jsonify({"detail": "Producto no encontrado"}), 404
        db.session.delete(p)
        db.session.commit()
        return jsonify({"ok": True}), 200
    except Exception as e:
        db.session.rollback()
        app.logger.exception(e)
        return jsonify({"detail": str(e)}), 500

# -----------------------------------
# Generación de QR (con fallback)
# -----------------------------------
def _png_fallback_base64(texto: str) -> str:
    """
    Devuelve un PNG mínimo válido con el texto en metadata (fallback sin PIL/qrcode).
    Usa un PNG de 1x1 transparente embebido.
    """
    # PNG 1x1 transparente base64
    tiny_png_base64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIW2P8z/CfBwAD"
        "gwG0k8x7WQAAAABJRU5ErkJggg=="
    )
    return tiny_png_base64

@app.get("/api/generar_qr/<int:pid>")
def generar_qr(pid):
    """
    Genera un QR base64 de un payload simple. Si no están instaladas
    librerías de QR, devuelve un PNG 1x1 (fallback) para no romper el front.
    Soporta ?slot_id= opcional (no usado en el contenido del QR aquí).
    """
    try:
        slot_id = request.args.get("slot_id", type=int)

        # Payload del QR (ajustá según tu lógica real de pagos)
        payload = {
            "producto_id": pid,
            "slot_id": slot_id,
            "ts": datetime.utcnow().isoformat()
        }

        # Intentar con qrcode + Pillow
        try:
            import qrcode
            from PIL import Image
            img = qrcode.make(str(payload))
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
            return jsonify({"qr_base64": b64}), 200
        except Exception:
            # Fallback: PNG mínimo
            b64 = _png_fallback_base64(str(payload))
            return jsonify({"qr_base64": b64, "note": "fallback_png"}), 200

    except Exception as e:
        app.logger.exception(e)
        return jsonify({"detail": str(e)}), 500


# -----------------------------------
# Main
# -----------------------------------
if __name__ == "__main__":
    # Para desarrollo local
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
