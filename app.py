# app.py
import os
import json
from datetime import datetime

from flask import Flask, jsonify, request, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from sqlalchemy import text, UniqueConstraint

import mercadopago

# -------------------------------------------------
# App / DB
# -------------------------------------------------
app = Flask(__name__, static_url_path="", static_folder="static")

CORS(app, resources={r"/api/*": {"origins": "*"}, r"/webhook": {"origins": "*"}})

DATABASE_URL = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL no configurada")

# compatibilidad con postgres://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

# -------------------------------------------------
# Modelos
# -------------------------------------------------
class Producto(db.Model):
    __tablename__ = "producto"

    id = db.Column(db.Integer, primary_key=True, index=True)
    slot_id = db.Column(db.Integer, nullable=False, index=True)
    nombre = db.Column(db.String(120), nullable=False, default="")
    precio = db.Column(db.Float, nullable=False, default=0.0)  # ARS, sin centavos si querés
    cantidad = db.Column(db.Integer, nullable=False, default=1)  # litros/ml u otra unidad
    habilitado = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("slot_id", name="uniq_producto_slot"),
    )

    def to_dict(self):
        return {
            "id": self.id,
            "slot_id": self.slot_id,
            "nombre": self.nombre,
            "precio": float(self.precio),
            "cantidad": self.cantidad,
            "habilitado": self.habilitado,
        }


class Pago(db.Model):
    __tablename__ = "pago"

    id = db.Column(db.Integer, primary_key=True)
    id_pago = db.Column(db.String(64), nullable=False, unique=True, index=True)
    estado = db.Column(db.String(32), nullable=False, default="pendiente")
    producto = db.Column(db.String(120), nullable=True)
    slot_id = db.Column(db.Integer, nullable=True)
    monto = db.Column(db.Float, nullable=True)
    raw = db.Column(db.Text, nullable=True)
    dispensado = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


# -------------------------------------------------
# Esquema (Railway/Postgres: idempotente)
# -------------------------------------------------
def ensure_schema():
    """Crea tablas y agrega columnas/índices si faltan (idempotente)."""
    db.create_all()

    # Solo para Postgres
    try:
        dialect = db.engine.url.get_backend_name()
    except Exception:
        dialect = ""

    if dialect.startswith("postgres"):
        stmts = [
            # Producto
            "ALTER TABLE producto ADD COLUMN IF NOT EXISTS slot_id INTEGER NOT NULL DEFAULT 0;",
            "ALTER TABLE producto ADD COLUMN IF NOT EXISTS nombre VARCHAR(120) NOT NULL DEFAULT '';",
            "ALTER TABLE producto ADD COLUMN IF NOT EXISTS precio DOUBLE PRECISION NOT NULL DEFAULT 0;",
            "ALTER TABLE producto ADD COLUMN IF NOT EXISTS cantidad INTEGER NOT NULL DEFAULT 1;",
            "ALTER TABLE producto ADD COLUMN IF NOT EXISTS habilitado BOOLEAN NOT NULL DEFAULT FALSE;",
            "ALTER TABLE producto ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW();",
            """DO $$
            BEGIN
              IF NOT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = 'uniq_producto_slot') THEN
                CREATE UNIQUE INDEX uniq_producto_slot ON producto(slot_id);
              END IF;
            END; $$;""",
            # Pago
            "ALTER TABLE pago ADD COLUMN IF NOT EXISTS id_pago VARCHAR(64) NOT NULL;",
            "ALTER TABLE pago ADD COLUMN IF NOT EXISTS estado VARCHAR(32) NOT NULL DEFAULT 'pendiente';",
            "ALTER TABLE pago ADD COLUMN IF NOT EXISTS producto VARCHAR(120);",
            "ALTER TABLE pago ADD COLUMN IF NOT EXISTS slot_id INTEGER;",
            "ALTER TABLE pago ADD COLUMN IF NOT EXISTS monto DOUBLE PRECISION;",
            "ALTER TABLE pago ADD COLUMN IF NOT EXISTS raw TEXT;",
            "ALTER TABLE pago ADD COLUMN IF NOT EXISTS dispensado BOOLEAN NOT NULL DEFAULT FALSE;",
            "ALTER TABLE pago ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW();",
            """DO $$
            BEGIN
              IF NOT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = 'pago_id_pago_key') THEN
                CREATE UNIQUE INDEX pago_id_pago_key ON pago(id_pago);
              END IF;
            END; $$;""",
        ]
        with db.engine.begin() as conn:
            for s in stmts:
                conn.execute(text(s))


with app.app_context():
    ensure_schema()

# -------------------------------------------------
# Utilidades
# -------------------------------------------------
def base_url() -> str:
    # Siempre absoluta (requerido por MP)
    # Ej: https://web-production-xxxx.up.railway.app/
    return request.host_url

def get_mp_sdk():
    access = os.environ.get("MP_ACCESS_TOKEN")
    if not access:
        return None
    try:
        return mercadopago.SDK(access)
    except Exception:
        return None


# -------------------------------------------------
# Rutas API
# -------------------------------------------------

@app.route("/api/productos", methods=["GET"])
def listar_productos():
    ps = Producto.query.order_by(Producto.slot_id.asc()).all()
    return jsonify([p.to_dict() for p in ps])


@app.route("/api/productos/<int:slot_id>", methods=["POST"])
def upsert_producto(slot_id: int):
    data = request.get_json(silent=True) or {}
    nombre = (data.get("nombre") or "").strip()
    precio = float(data.get("precio") or 0)
    cantidad = int(data.get("cantidad") or 1)
    habilitado = bool(data.get("habilitado") or False)

    p = Producto.query.filter_by(slot_id=slot_id).first()
    if not p:
        p = Producto(slot_id=slot_id)

    p.nombre = nombre
    p.precio = precio
    p.cantidad = cantidad
    p.habilitado = habilitado

    db.session.add(p)
    db.session.commit()
    return jsonify(p.to_dict())


@app.route("/api/productos/<int:slot_id>", methods=["DELETE"])
def delete_producto(slot_id: int):
    p = Producto.query.filter_by(slot_id=slot_id).first()
    if not p:
        # Idempotente
        return jsonify({"ok": True})
    db.session.delete(p)
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/api/generar_qr/<int:slot_id>", methods=["GET"])
def generar_qr(slot_id: int):
    sdk = get_mp_sdk()
    if sdk is None:
        return jsonify({"error": "MP_ACCESS_TOKEN no configurado"}), 500

    p = Producto.query.filter_by(slot_id=slot_id).first()
    if (not p) or (not p.habilitado) or (not p.nombre) or (p.precio is None) or (p.precio <= 0):
        return jsonify({"error": "no se pudo crear preferencia (¿producto habilitado y con nombre/precio?)"}), 400

    # Back URLs ABSOLUTAS (requerido por MP)
    base = base_url().rstrip("/")
    back_urls = {
        "success": f"{base}/",
        "pending": f"{base}/",
        "failure": f"{base}/",
    }

    preference_data = {
        "items": [
            {
                "title": p.nombre,
                "quantity": 1,
                "currency_id": "ARS",
                "unit_price": float(p.precio),
                "description": p.nombre,
            }
        ],
        "metadata": {
            "producto_id": p.id,
            "slot_id": p.slot_id,
        },
        "external_reference": f"prod:{p.id}",
        "notification_url": f"{base}/webhook",
        "back_urls": back_urls,
        "auto_return": "approved",
    }

    app.logger.info(f"[MP] Creando preferencia -> {json.dumps(preference_data)}")

    pref = sdk.preference().create(preference_data)
    status = pref.get("status")
    resp = pref.get("response")

    if status != 201:
        app.logger.error(f"[MP] error pref -> status={status}, detalle={resp}")
        return jsonify({"error": "no se pudo crear preferencia", "detalle": resp}), 400

    # Devolvemos el link para QR/redirect
    init_point = resp.get("init_point") or resp.get("sandbox_init_point")
    return jsonify({"qr_link": init_point})


@app.route("/webhook", methods=["POST"])
def webhook():
    """Recibe notificaciones de Mercado Pago y guarda en DB (tabla pago)."""
    try:
        payload = request.get_json(silent=True) or {}
        topic = payload.get("type") or payload.get("topic") or ""
        data = payload.get("data") or {}
        id_pago = str(data.get("id") or payload.get("id") or "TEST_PAYMENT_123")

        # Estado básico según topic/evento
        estado = "pendiente"
        if topic in ("payment", "payment.updated", "merchant_order"):
            # Podrías consultar el detalle vía SDK si querés enriquecer.
            estado = "approved" if "approved" in json.dumps(payload).lower() else "pendiente"

        meta = {}
        try:
            meta = payload.get("metadata") or {}
        except Exception:
            pass

        # Intento de producto/slot desde metadatos
        producto = meta.get("producto") or ""
        slot_id = meta.get("slot_id")

        row = Pago(
            id_pago=id_pago,
            estado=estado,
            producto=producto,
            slot_id=slot_id,
            monto=None,
            raw=json.dumps(payload, ensure_ascii=False),
            dispensado=False,
        )
        db.session.add(row)
        db.session.commit()

        app.logger.info(f"[WEBHOOK] guardado {id_pago} estado={estado}")
        return jsonify({"ok": True})
    except Exception as e:
        # Evitar crash: log y 200 para que MP no reintente infinito
        app.logger.exception(f"[WEBHOOK] error: {e}")
        return jsonify({"ok": True})


# -------------------------------------------------
# Front static (opcional)
# -------------------------------------------------
@app.route("/", methods=["GET"])
def index():
    # Si tenés un build del front en /static, sirve index.html
    index_path = os.path.join(app.static_folder, "index.html")
    if os.path.exists(index_path):
        return send_from_directory(app.static_folder, "index.html")
    return "OK", 200


# -------------------------------------------------
# Main (local)
# -------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
