import os
import json
from datetime import datetime

from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from sqlalchemy import text
import requests

# ---------- App & DB ----------
app = Flask(__name__)
CORS(app)

# DATABASE_URL de Railway (puede venir como postgres://)
db_url = os.getenv("DATABASE_URL", "").strip()
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)
if not db_url:
    # fallback local
    db_url = "sqlite:///productos.db"

app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

# ---------- Modelos ----------
class Producto(db.Model):
    __tablename__ = "producto"
    id = db.Column(db.Integer, primary_key=True)
    slot_id = db.Column(db.Integer, unique=True, index=True, nullable=False)
    nombre = db.Column(db.String(120), nullable=False, default="")
    precio = db.Column(db.Float, nullable=False, default=0.0)  # entero o decimal
    cantidad = db.Column(db.Integer, nullable=False, default=1)
    habilitado = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "slot_id": self.slot_id,
            "nombre": self.nombre,
            "precio": int(self.precio) if self.precio.is_integer() else self.precio,
            "cantidad": self.cantidad,
            "habilitado": self.habilitado,
        }

class Pago(db.Model):
    __tablename__ = "pago"
    id = db.Column(db.Integer, primary_key=True)
    id_pago = db.Column(db.String(64), unique=True, index=True, nullable=False)
    estado = db.Column(db.String(32), nullable=False, default="pendiente")
    producto = db.Column(db.String(200), nullable=True)
    slot_id = db.Column(db.Integer, nullable=True)
    monto = db.Column(db.Float, nullable=True)
    raw = db.Column(db.JSON, nullable=True)
    dispensado = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

# ---------- Esquema (crea/ajusta en Railway) ----------
def ensure_schema():
    with app.app_context():
        db.create_all()
        # Si es Postgres, reforzamos columnas e índice único por si la tabla ya existía
        if app.config["SQLALCHEMY_DATABASE_URI"].startswith("postgresql"):
            stmts = [
                # producto
                "ALTER TABLE producto ADD COLUMN IF NOT EXISTS slot_id INTEGER;",
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
                # pago
                "ALTER TABLE pago ADD COLUMN IF NOT EXISTS id_pago VARCHAR(64);",
                "ALTER TABLE pago ADD COLUMN IF NOT EXISTS estado VARCHAR(32) NOT NULL DEFAULT 'pendiente';",
                "ALTER TABLE pago ADD COLUMN IF NOT EXISTS producto VARCHAR(200);",
                "ALTER TABLE pago ADD COLUMN IF NOT EXISTS slot_id INTEGER;",
                "ALTER TABLE pago ADD COLUMN IF NOT EXISTS monto DOUBLE PRECISION;",
                "ALTER TABLE pago ADD COLUMN IF NOT EXISTS raw JSON;",
                "ALTER TABLE pago ADD COLUMN IF NOT EXISTS dispensado BOOLEAN NOT NULL DEFAULT FALSE;",
                "ALTER TABLE pago ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW();",
                "CREATE UNIQUE INDEX IF NOT EXISTS pago_id_pago_key ON pago(id_pago);",
            ]
            for s in stmts:
                db.session.execute(text(s))
            db.session.commit()

ensure_schema()

# ---------- Utils ----------
def base_url():
    # https://host/ -> sin / final
    root = request.url_root
    return root[:-1] if root.endswith("/") else root

def get_mp_token():
    tok = os.getenv("MP_ACCESS_TOKEN", "").strip()
    return tok or None

# ---------- Rutas ----------
@app.get("/")
def health():
    return "OK", 200

# --- CRUD Productos ---
@app.get("/api/productos")
def get_productos():
    ps = Producto.query.order_by(Producto.slot_id.asc()).all()
    return jsonify([p.to_dict() for p in ps])

@app.post("/api/productos/<int:slot_id>")
def upsert_producto(slot_id: int):
    data = request.get_json(force=True, silent=True) or {}
    nombre = (data.get("nombre") or "").strip()
    precio = float(data.get("precio") or 0)
    cantidad = int(data.get("cantidad") or 1)
    habilitado = bool(data.get("habilitado") or False)

    p = Producto.query.filter_by(slot_id=slot_id).first()
    if not p:
        p = Producto(slot_id=slot_id)
        db.session.add(p)

    p.nombre = nombre
    p.precio = precio
    p.cantidad = cantidad
    p.habilitado = habilitado
    db.session.commit()
    return jsonify({"ok": True, "producto": p.to_dict()})

@app.delete("/api/productos/<int:slot_id>")
def delete_producto(slot_id: int):
    p = Producto.query.filter_by(slot_id=slot_id).first()
    if not p:
        return jsonify({"ok": True})  # idempotente
    db.session.delete(p)
    db.session.commit()
    return jsonify({"ok": True})

# --- Generar QR / Preferencia MP ---
@app.route('/api/generar_qr/<int:producto_id>', methods=['POST'])
def generar_qr(producto_id):
    producto = Producto.query.get(producto_id)

    if not producto:
        return jsonify({"error": "Producto no encontrado"}), 404

    # Log para debugging
    print(f"Generando QR para producto: id={producto.id}, nombre={producto.nombre}, precio={producto.precio}, habilitado={producto.habilitado}")

    if not producto.habilitado or not producto.nombre or producto.precio <= 0:
        return jsonify({"error": "Producto no válido para generar QR"}), 400

    try:
        preference_data = {
            "items": [{
                "title": producto.nombre,
                "quantity": 1,
                "currency_id": "ARS",
                "unit_price": float(producto.precio)
            }],
            "back_urls": {
                "success": "https://tu-dominio/success",
                "failure": "https://tu-dominio/failure",
                "pending": "https://tu-dominio/pending"
            },
            "auto_return": "approved",
            "notification_url": "https://web-production-e7d2.up.railway.app/webhook"
        }

        sdk = mercadopago.SDK(os.environ["MP_ACCESS_TOKEN"])
        preference = sdk.preference().create(preference_data)

        return jsonify({"init_point": preference["response"]["init_point"]})

    except Exception as e:
        print(f"Error al generar QR: {e}")
        return jsonify({"error": str(e)}), 500

    # Datos de preferencia
    back = base_url()
    pref = {
        "items": [
            {
                "title": p.nombre,
                "quantity": 1,
                "unit_price": float(p.precio),
            }
        ],
        "external_reference": f"prod:{p.id}",
        "notification_url": f"{back}/webhook",
        "back_urls": {
            "success": f"{back}/",
            "pending": f"{back}/",
            "failure": f"{back}/",
        },
        "auto_return": "approved",
        "metadata": {
            "producto_id": p.id,
            "slot_id": p.slot_id,
            "nombre": p.nombre,
        },
    }

    # Llamada directa a la API (estable y simple)
    r = requests.post(
        "https://api.mercadopago.com/checkout/preferences",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        data=json.dumps(pref),
        timeout=20,
    )
    if r.status_code not in (200, 201):
        try:
            det = r.json()
        except Exception:
            det = {"msg": r.text}
        return jsonify({"error": "no se pudo crear preferencia", "detalle": det}), 400

    resp = r.json()
    # Campos útiles: init_point (redir), sandbox_init_point, qr_code (si viene)
    qr = None
    try:
        qr = resp["point_of_interaction"]["transaction_data"]["qr_code"]
    except Exception:
        pass

    return jsonify({
        "ok": True,
        "preference_id": resp.get("id"),
        "init_point": resp.get("init_point"),
        "sandbox_init_point": resp.get("sandbox_init_point"),
        "qr": qr
    })

# --- Webhook MP ---
@app.post("/webhook")
def webhook():
    token = get_mp_token()
    if not token:
        return jsonify({"ok": True})  # evitar reintentos molestos

    payload = request.get_json(silent=True) or {}
    if not payload and request.form:
        # A veces llega como x-www-form-urlencoded
        try:
            payload = {k: request.form.get(k) for k in request.form.keys()}
            # si "data" viene como string JSON, parsearlo
            if isinstance(payload.get("data"), str):
                payload["data"] = json.loads(payload["data"])
        except Exception:
            payload = {}

    # Identificar payment_id
    payment_id = None
    if isinstance(payload.get("data"), dict) and payload["data"].get("id"):
        payment_id = str(payload["data"]["id"])
    elif payload.get("id") and payload.get("type") == "payment":
        payment_id = str(payload["id"])

    if not payment_id:
        # merchant_order u otros: responder OK igual
        return jsonify({"ok": True})

    # Consultar el pago
    url = f"https://api.mercadopago.com/v1/payments/{payment_id}"
    r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=20)
    if r.status_code != 200:
        return jsonify({"ok": True})

    pay = r.json()
    estado = pay.get("status") or "pendiente"
    monto = pay.get("transaction_amount")
    title = None
    try:
        items = pay.get("additional_info", {}).get("items") or []
        if items:
            title = items[0].get("title")
    except Exception:
        pass
    meta = pay.get("metadata") or {}
    slot_id = meta.get("slot_id")

    # Upsert del pago
    pg = Pago.query.filter_by(id_pago=str(payment_id)).first()
    if not pg:
        pg = Pago(id_pago=str(payment_id))
        db.session.add(pg)

    pg.estado = estado
    pg.monto = monto
    pg.producto = title or meta.get("nombre") or "Producto"
    try:
        pg.slot_id = int(slot_id) if slot_id is not None else None
    except Exception:
        pg.slot_id = None
    pg.raw = pay

    db.session.commit()
    return jsonify({"ok": True})

# ---------- Run (local) ----------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
