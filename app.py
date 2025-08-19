import os
import json
from datetime import datetime

from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from sqlalchemy import text, UniqueConstraint

# SDK oficial de Mercado Pago
import mercadopago


# -----------------------------
# Config Flask
# -----------------------------

app = Flask(__name__)
CORS(app)

# DATABASE_URL de Railway (puede venir con "postgres://", lo normalizamos a "postgresql://")
db_url = os.getenv("DATABASE_URL", "")
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)


# -----------------------------
# Modelos
# -----------------------------

class Producto(db.Model):
    __tablename__ = "producto"
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    slot_id = db.Column(db.Integer, nullable=False, index=True, default=0)
    nombre = db.Column(db.String(120), nullable=False, default="")
    precio = db.Column(db.Float, nullable=False, default=0.0)  # si querés centavos cambiá a Numeric
    cantidad = db.Column(db.Integer, nullable=False, default=1)  # litros/ml
    habilitado = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        # cada slot tiene 0..1 producto
        db.Index("idx_producto_slot", "slot_id", unique=True),
    )

    def to_dict(self):
        return {
            "id": self.id,
            "slot_id": self.slot_id,
            "nombre": self.nombre,
            "precio": self.precio,
            "cantidad": self.cantidad,
            "habilitado": self.habilitado,
        }


class Pago(db.Model):
    __tablename__ = "pago"
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    id_pago = db.Column(db.String(64), nullable=False)        # id de MP o merchant_order / payment
    estado = db.Column(db.String(32), nullable=False, default="pendiente")
    producto = db.Column(db.String(180), nullable=False, default="")
    slot_id = db.Column(db.Integer, nullable=False, default=0)
    monto = db.Column(db.Float, nullable=False, default=0.0)
    raw = db.Column(db.Text, nullable=True)
    dispensado = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("id_pago", name="pago_id_pago_key"),
    )


# -----------------------------
# Esquema / migración simple
# -----------------------------

def ensure_schema():
    """
    Crea tablas y agrega columnas/índices si faltan (idempotente),
    útil para Postgres en Railway cuando ya hay una base creada.
    """
    from sqlalchemy import inspect

    # Crear tablas si no existen
    db.create_all()

    dialect = db.engine.url.get_backend_name()
    if not dialect.startswith("postgres"):
        return

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
           IF NOT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname='idx_producto_slot') THEN
               CREATE UNIQUE INDEX idx_producto_slot ON producto(slot_id);
           END IF;
        END $$;""",
        # Pago
        "ALTER TABLE pago ADD COLUMN IF NOT EXISTS id_pago VARCHAR(64) NOT NULL;",
        "ALTER TABLE pago ADD COLUMN IF NOT EXISTS estado VARCHAR(32) NOT NULL DEFAULT 'pendiente';",
        "ALTER TABLE pago ADD COLUMN IF NOT EXISTS producto VARCHAR(180) NOT NULL DEFAULT '';",
        "ALTER TABLE pago ADD COLUMN IF NOT EXISTS slot_id INTEGER NOT NULL DEFAULT 0;",
        "ALTER TABLE pago ADD COLUMN IF NOT EXISTS monto DOUBLE PRECISION NOT NULL DEFAULT 0;",
        "ALTER TABLE pago ADD COLUMN IF NOT EXISTS raw TEXT;",
        "ALTER TABLE pago ADD COLUMN IF NOT EXISTS dispensado BOOLEAN NOT NULL DEFAULT FALSE;",
        "ALTER TABLE pago ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW();",
        """DO $$
        BEGIN
           IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='pago_id_pago_key') THEN
               ALTER TABLE pago ADD CONSTRAINT pago_id_pago_key UNIQUE (id_pago);
           END IF;
        END $$;""",
    ]

    with db.engine.begin() as conn:
        for s in stmts:
            conn.execute(text(s))


with app.app_context():
    ensure_schema()


# -----------------------------
# Utilidades
# -----------------------------

def base_url():
    # Ej: "https://web-production-xxxx.up.railway.app"
    url = request.host_url  # trae barra final y podría venir en http
    if url.startswith("http://"):
        url = url.replace("http://", "https://", 1)
    return url.rstrip("/")


def get_mp_sdk():
    access = os.environ.get("MP_ACCESS_TOKEN")
    if not access:
        raise RuntimeError("MP_ACCESS_TOKEN no configurado")
    return mercadopago.SDK(access)


# -----------------------------
# Páginas de retorno de MP
# -----------------------------

@app.get("/success")
def mp_success():
    return "Pago aprobado", 200

@app.get("/failure")
def mp_failure():
    return "Pago rechazado", 200

@app.get("/pending")
def mp_pending():
    return "Pago pendiente", 200


# -----------------------------
# Rutas básicas
# -----------------------------

@app.get("/")
def root():
    return "OK", 200


# -----------------------------
# CRUD Productos (simple)
# -----------------------------

@app.get("/api/productos")
def listar_productos():
    items = Producto.query.order_by(Producto.slot_id.asc()).all()
    return jsonify([p.to_dict() for p in items])

@app.post("/api/productos/<int:slot_id>")
def upsert_producto(slot_id: int):
    body = request.get_json(silent=True) or {}
    nombre = (body.get("nombre") or "").strip()
    precio = float(body.get("precio") or 0)
    cantidad = int(body.get("cantidad") or 1)
    habilitado = bool(body.get("habilitado"))

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

@app.delete("/api/productos/<int:slot_id>")
def delete_producto(slot_id: int):
    p = Producto.query.filter_by(slot_id=slot_id).first()
    if not p:
        return jsonify({"ok": True})  # idempotente
    db.session.delete(p)
    db.session.commit()
    return jsonify({"ok": True})


# -----------------------------
# Generar QR (Mercado Pago)
# -----------------------------

@app.post("/api/generar_qr/<int:slot_id>")
def generar_qr(slot_id: int):
    p = Producto.query.filter_by(slot_id=slot_id).first()
    if not p or not p.habilitado or not p.nombre or p.precio <= 0:
        return jsonify({"error": "producto inválido (habilitado/nombre/precio)"}), 400

    try:
        sdk = get_mp_sdk()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    base = base_url()
    preference_data = {
        "items": [{
            "title": p.nombre,
            "quantity": 1,
            "unit_price": float(p.precio),
            "currency_id": "ARS",
            "description": p.nombre,
        }],
        "external_reference": f"prod:{p.id}",
        "metadata": {"producto_id": p.id, "slot_id": p.slot_id},
        "notification_url": f"{base}/webhook",
        "back_urls": {
            "success": f"{base}/success",
            "failure": f"{base}/failure",
            "pending": f"{base}/pending",
        },
        "auto_return": "approved",
    }

    print("[MP] Creando preferencia ->", preference_data, flush=True)
    pref = sdk.preference().create(preference_data)

    if pref.get("status") != 201:
        detalle = pref.get("response")
        print("[MP] error pref:", pref.get("status"), detalle, flush=True)
        return jsonify({"error": "no se pudo crear preferencia", "detalle": detalle}), 400

    init_point = pref["response"]["init_point"]
    return jsonify({"init_point": init_point})


# -----------------------------
# Webhook Mercado Pago
# -----------------------------

@app.post("/webhook")
def webhook():
    # Mercado Pago envía JSON con campos como: id, type, data:{id}
    data = request.get_json(silent=True) or {}
    print("[webhook] raw:", json.dumps(data, ensure_ascii=False), flush=True)

    # Guardamos un registro básico (idempotente por id_pago UNIQUE)
    try:
        pago_id = str(data.get("id") or data.get("data", {}).get("id") or "")
        if not pago_id:
            pago_id = f"TEST_{datetime.utcnow().timestamp()}"

        # Para este ejemplo dejamos estado como 'pendiente' (luego podrías consultar a la API de MP)
        registro = Pago(
            id_pago=pago_id,
            estado="pendiente",
            producto="",
            slot_id=0,
            monto=0,
            raw=json.dumps(data, ensure_ascii=False),
            dispensado=False,
        )
        db.session.add(registro)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        # Si es violación de UNIQUE, lo ignoramos para no fallar
        print("[DB] error guardando pago:", repr(e), flush=True)

    return jsonify({"ok": True})


# -----------------------------
# Main (Gunicorn usará 'app')
# -----------------------------

if __name__ == "__main__":
    # Útil para correr local: FLASK_ENV=development python app.py
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
