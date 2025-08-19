import os
import json
from datetime import datetime

from flask import Flask, request, jsonify, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from sqlalchemy import text, String, Integer, Boolean, DateTime, Float
from sqlalchemy.exc import SQLAlchemyError

# SDK Mercado Pago (pip install mercadopago)
import mercadopago

# -------------------------------------------------
# Config Flask / DB
# -------------------------------------------------
app = Flask(__name__)
CORS(app)

DATABASE_URL = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL no está configurada")

# Compatibilidad con postgres:// -> postgresql+psycopg2://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg2://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

# -------------------------------------------------
# Modelos
# -------------------------------------------------
class Producto(db.Model):
    __tablename__ = "producto"
    id = db.Column(Integer, primary_key=True, index=True)
    slot_id = db.Column(Integer, nullable=False, default=0)
    nombre = db.Column(String(120), nullable=False, default="")
    precio = db.Column(Float, nullable=False, default=0.0)  # enteros o con decimales
    cantidad = db.Column(Integer, nullable=False, default=1)
    habilitado = db.Column(Boolean, nullable=False, default=False)
    created_at = db.Column(DateTime, nullable=False, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "slot_id": self.slot_id,
            "nombre": self.nombre,
            "precio": float(self.precio or 0),
            "cantidad": self.cantidad,
            "habilitado": bool(self.habilitado),
        }


class Pago(db.Model):
    __tablename__ = "pago"
    id = db.Column(Integer, primary_key=True)
    id_pago = db.Column(String(64), unique=True, index=True)       # MP payment id / external id
    estado = db.Column(String(32), default="pendiente")
    producto = db.Column(String(120))
    slot_id = db.Column(Integer, default=0)
    monto = db.Column(Float, default=0.0)
    raw = db.Column(String)     # JSON en texto
    dispensado = db.Column(Boolean, default=False)
    created_at = db.Column(DateTime, default=datetime.utcnow, nullable=False)

# -------------------------------------------------
# Crear/ajustar esquema (idempotente para Railway)
# -------------------------------------------------
def ensure_schema():
    """Crea tablas y agrega columnas/índices si faltan (seguro de ejecutar varias veces)."""
    dialect = db.engine.url.get_backend_name()
    db.create_all()

    if str(dialect).startswith("postgres"):
        stmts = [
            # producto
            "ALTER TABLE producto ADD COLUMN IF NOT EXISTS slot_id INTEGER NOT NULL DEFAULT 0;",
            "ALTER TABLE producto ADD COLUMN IF NOT EXISTS nombre VARCHAR(120) NOT NULL DEFAULT '';",
            "ALTER TABLE producto ADD COLUMN IF NOT EXISTS precio DOUBLE PRECISION NOT NULL DEFAULT 0;",
            "ALTER TABLE producto ADD COLUMN IF NOT EXISTS cantidad INTEGER NOT NULL DEFAULT 1;",
            "ALTER TABLE producto ADD COLUMN IF NOT EXISTS habilitado BOOLEAN NOT NULL DEFAULT FALSE;",
            "ALTER TABLE producto ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now();",
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname='idx_producto_slot') THEN "
            "CREATE UNIQUE INDEX idx_producto_slot ON producto(slot_id); END IF; END $$;",
            # pago
            "ALTER TABLE pago ADD COLUMN IF NOT EXISTS id_pago VARCHAR(64);",
            "ALTER TABLE pago ADD COLUMN IF NOT EXISTS estado VARCHAR(32) DEFAULT 'pendiente';",
            "ALTER TABLE pago ADD COLUMN IF NOT EXISTS producto VARCHAR(120);",
            "ALTER TABLE pago ADD COLUMN IF NOT EXISTS slot_id INTEGER DEFAULT 0;",
            "ALTER TABLE pago ADD COLUMN IF NOT EXISTS monto DOUBLE PRECISION DEFAULT 0;",
            "ALTER TABLE pago ADD COLUMN IF NOT EXISTS raw TEXT;",
            "ALTER TABLE pago ADD COLUMN IF NOT EXISTS dispensado BOOLEAN DEFAULT FALSE;",
            "ALTER TABLE pago ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now();",
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'pago_id_pago_key') THEN "
            "ALTER TABLE pago ADD CONSTRAINT pago_id_pago_key UNIQUE (id_pago); END IF; END $$;",
        ]
        with db.engine.begin() as conn:
            for s in stmts:
                conn.execute(text(s))

with app.app_context():
    ensure_schema()

# -------------------------------------------------
# Utils
# -------------------------------------------------
def get_mp_sdk():
    access = os.environ.get("MP_ACCESS_TOKEN")
    if not access:
        return None
    return mercadopago.SDK(access)

def base_url() -> str:
    """
    URL pública del backend, sin barra final.
    Prioriza PUBLIC_BASE_URL; si no, usa request.host_url.
    """
    fixed = os.environ.get("PUBLIC_BASE_URL")
    if fixed and fixed.strip():
        return fixed.rstrip("/")
    # request.host_url sólo existe dentro del request
    try:
        return request.host_url.rstrip("/")
    except RuntimeError:
        # fuera de request; último recurso: Railway env var RAILWAY_PUBLIC_DOMAIN si existiera
        dom = os.environ.get("RAILWAY_PUBLIC_DOMAIN")
        return f"https://{dom}".rstrip("/") if dom else ""

# -------------------------------------------------
# Productos API
# -------------------------------------------------
@app.get("/api/productos")
def list_productos():
    prods = Producto.query.order_by(Producto.slot_id.asc()).all()
    return jsonify([p.to_dict() for p in prods])

@app.post("/api/productos/<int:slot_id>")
def upsert_producto(slot_id: int):
    data = request.get_json(force=True) or {}

    p = Producto.query.filter_by(slot_id=slot_id).first()
    if not p:
        p = Producto(slot_id=slot_id)
        db.session.add(p)

    p.nombre = (data.get("nombre") or "").strip()
    p.precio = float(data.get("precio") or 0)
    p.cantidad = int(data.get("cantidad") or 1)
    p.habilitado = bool(data.get("habilitado"))
    db.session.commit()

    return jsonify(p.to_dict())

@app.delete("/api/productos/<int:slot_id>")
def delete_producto(slot_id: int):
    p = Producto.query.filter_by(slot_id=slot_id).first()
    if not p:
        return jsonify({"ok": True})
    db.session.delete(p)
    db.session.commit()
    return jsonify({"ok": True})

# -------------------------------------------------
# Rutas de retorno para Mercado Pago
# -------------------------------------------------
@app.get("/success")
def pago_success():
    return "Pago aprobado ✅"

@app.get("/pending")
def pago_pending():
    return "Pago pendiente ⏳"

@app.get("/failure")
def pago_failure():
    return "Pago fallido ❌"

# -------------------------------------------------
# Generar QR / Preferencia (por ID de producto)
# -------------------------------------------------
# --- Generar QR por slot_id (con fallback por id) ---
@app.get("/api/generar_qr/<int:slot_or_id>")
def generar_qr(slot_or_id: int):
    sdk = get_mp_sdk()
    if sdk is None:
        return jsonify({"error": "MP_ACCESS_TOKEN no configurado"}), 500

    # 1) Buscar por slot_id; si no hay, buscar por id
    p = Producto.query.filter_by(slot_id=slot_or_id).first()
    if not p:
        p = Producto.query.filter_by(id=slot_or_id).first()

    # 2) Validaciones mínimas
    if not p or not p.habilitado or not p.nombre or (p.precio is None) or (float(p.precio) <= 0):
        return jsonify({"error": "no se pudo crear preferencia (¿producto habilitado y con nombre/precio?)"}), 400

    # 3) Armar preferencia MP (completa: currency_id, back_urls, auto_return, notification_url)
    base = base_url().rstrip("/")
    preference_data = {
        "items": [{
            "title": p.nombre,
            "quantity": 1,
            "unit_price": float(p.precio),
            "currency_id": "ARS",
            "description": f"slot {p.slot_id}"
        }],
        "external_reference": f"prod:{p.id}",
        "back_urls": {
            "success": f"{base}/",
            "failure": f"{base}/",
            "pending": f"{base}/"
        },
        "auto_return": "approved",
        "notification_url": f"{base}/webhook",
        "metadata": {
            "producto_id": p.id,
            "slot_id": p.slot_id
        }
    }

    try:
        pref = sdk.preference().create(preference_data)
        resp = pref.get("response", {})
        link = resp.get("init_point") or resp.get("sandbox_init_point")
        pref_id = resp.get("id")

        if not link:
            return jsonify({"error": "MP no devolvió init_point"}), 500

        return jsonify({"ok": True, "pref_id": pref_id, "qr_link": link})
    except Exception as e:
        return jsonify({"error": f"error creando preferencia: {e}"}), 500

# -------------------------------------------------
# Webhook (POST)
# -------------------------------------------------
@app.post("/webhook")
def webhook():
    try:
        payload = request.get_json(silent=True) or {}
        print("[WEBHOOK] payload:", payload)

        topic = (payload.get("type") or payload.get("topic") or "").lower()
        data = payload.get("data") or {}
        payment_id = str(data.get("id") or data.get("payment_id") or "").strip()

        # Guardar evento "crudo"
        rec = Pago.query.filter_by(id_pago=payment_id).first()
        if not rec:
            rec = Pago(id_pago=payment_id)
            db.session.add(rec)

        rec.raw = json.dumps(payload, ensure_ascii=False)
        # No sabemos el estado todavía; si viniera en el payload:
        estado = payload.get("action") or payload.get("status")
        if estado:
            rec.estado = str(estado)

        db.session.commit()
        return jsonify({"ok": True})
    except Exception as e:
        print("[WEBHOOK] error:", e)
        return jsonify({"ok": False}), 500

# -------------------------------------------------
# Root / Health
# -------------------------------------------------
@app.get("/")
def root():
    return "OK"

@app.get("/healthz")
def healthz():
    try:
        db.session.execute(text("SELECT 1"))
        return jsonify({"ok": True})
    except SQLAlchemyError as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# -------------------------------------------------
# Run local (opcional)
# -------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
