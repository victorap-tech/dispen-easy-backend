# app.py
import os
import json
import logging
from datetime import datetime

from flask import Flask, request, jsonify, redirect
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS

# --- Mercado Pago SDK ---
# pip install mercadopago
import mercadopago

app = Flask(__name__)
CORS(app)

# ---------------------------
# Config DB
# ---------------------------
DATABASE_URL = os.environ.get("DATABASE_URL", "")
if DATABASE_URL.startswith("postgres://"):
    # SQLAlchemy 2.x requiere postgresql://
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL or "sqlite:///local.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

# ---------------------------
# Modelos
# ---------------------------
class Producto(db.Model):
    __tablename__ = "producto"
    id = db.Column(db.Integer, primary_key=True)
    slot_id = db.Column(db.Integer, nullable=False, unique=True, index=True)
    nombre = db.Column(db.String(120), default="", nullable=False)
    precio = db.Column(db.Float, default=0.0, nullable=False)
    cantidad = db.Column(db.Integer, default=1, nullable=False)
    habilitado = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    def to_dict(self):
        return {
            "id": self.id,
            "slot_id": self.slot_id,
            "nombre": self.nombre,
            "precio": float(self.precio or 0),
            "cantidad": self.cantidad,
            "habilitado": self.habilitado,
        }


class Pago(db.Model):
    __tablename__ = "pago"
    id = db.Column(db.Integer, primary_key=True)
    id_pago = db.Column(db.String(64), unique=True, index=True)  # MP payment id
    estado = db.Column(db.String(32))
    producto = db.Column(db.String(160))
    slot_id = db.Column(db.Integer)
    monto = db.Column(db.Float)
    dispensado = db.Column(db.Boolean, default=False)
    raw = db.Column(db.Text)  # JSON del webhook
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


# ---------------------------
# Schema helper para Postgres
# ---------------------------
def ensure_schema():
    """Crea tablas y agrega columnas que falten (idempotente para Postgres)."""
    db.create_all()

    try:
        dialect = db.engine.url.get_backend_name()
    except Exception:
        dialect = ""

    if str(dialect).startswith("postgres"):
        from sqlalchemy import text
        stmts = [
            # columnas (por si el schema viene de una versión anterior)
            "ALTER TABLE producto ADD COLUMN IF NOT EXISTS cantidad INTEGER NOT NULL DEFAULT 1;",
            "ALTER TABLE producto ADD COLUMN IF NOT EXISTS habilitado BOOLEAN NOT NULL DEFAULT FALSE;",
            "ALTER TABLE producto ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW();",
            "ALTER TABLE pago ADD COLUMN IF NOT EXISTS dispensado BOOLEAN NOT NULL DEFAULT FALSE;",
            "ALTER TABLE pago ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW();",
            # índice único para slot
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname='idx_producto_slot') THEN "
            "CREATE UNIQUE INDEX idx_producto_slot ON producto(slot_id); END IF; END $$;",
        ]
        with db.engine.begin() as conn:
            for s in stmts:
                conn.execute(text(s))

with app.app_context():
    ensure_schema()

# ---------------------------
# Utilidades
# ---------------------------
def base_url() -> str:
    """
    Devuelve la base absoluta (con https) que pide MP para back_urls/notification_url.
    request.host_url ya viene con la barra final; normalizamos https.
    """
    host = request.host
    return f"https://{host}/"

def get_mp_sdk():
    access = os.environ.get("MP_ACCESS_TOKEN")
    if not access:
        return None, "MP_ACCESS_TOKEN no configurado"
    try:
        return mercadopago.SDK(access), None
    except Exception as e:
        return None, f"Error inicializando SDK: {e}"

def find_producto_by_id_or_slot(x: int):
    """Primero intenta por id, si no hay, intenta por slot_id."""
    p = Producto.query.filter_by(id=x).first()
    if p:
        return p
    return Producto.query.filter_by(slot_id=x).first()

# ---------------------------
# Rutas básicas
# ---------------------------
@app.get("/")
def root():
    return "DISPENEASY API OK"

@app.get("/success")
def return_success():
    return redirect("/")

@app.get("/failure")
def return_failure():
    return redirect("/")

@app.get("/pending")
def return_pending():
    return redirect("/")


# ---------------------------
# CRUD Productos
# ---------------------------
@app.get("/api/productos")
def listar_productos():
    ps = Producto.query.order_by(Producto.slot_id.asc()).all()
    return jsonify([p.to_dict() for p in ps])

@app.post("/api/productos/<int:slot_id>")
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

@app.delete("/api/productos/<int:slot_id>")
def delete_producto(slot_id: int):
    p = Producto.query.filter_by(slot_id=slot_id).first()
    if not p:
        # idempotente
        return jsonify({"ok": True})
    db.session.delete(p)
    db.session.commit()
    return jsonify({"ok": True})


# ---------------------------
# Generar QR / Preferencia
# ---------------------------
@app.get("/api/generar_qr/<int:x>")
def generar_qr(x: int):
    """
    x puede ser id de producto O slot_id.
    """
    sdk, err = get_mp_sdk()
    if err:
        return jsonify({"error": err}), 500

    p = find_producto_by_id_or_slot(x)
    if not p or not p.habilitado or not p.nombre or (p.precio or 0) <= 0:
        return jsonify({"error": "MP no devolvió init_point (¿producto habilitado y con nombre/precio?)"}), 400

    # Armar URLs absolutas válidas
    origin = base_url()
    back_urls = {
        "success": origin + "success",
        "failure": origin + "failure",
        "pending": origin + "pending",
    }

    preference_data = {
        "items": [
            {
                "title": p.nombre,
                "quantity": 1,
                "currency_id": "ARS",
                "unit_price": float(p.precio),
                "description": "Producto de DISPENEASY",
            }
        ],
        "back_urls": back_urls,
        "auto_return": "approved",
        "notification_url": origin + "webhook",
        "external_reference": f"prod:{p.id}:slot:{p.slot_id}",
        "metadata": {"producto_id": p.id, "slot_id": p.slot_id},
    }

    app.logger.info(f"[MP] Creando preferencia -> {json.dumps(preference_data)}")

    try:
        pref_res = sdk.preference().create(preference_data)
        status = pref_res.get("status")
        body = pref_res.get("response", {})
    except Exception as e:
        app.logger.exception("[MP] Error creando preferencia")
        return jsonify({"error": f"Excepción creando preferencia: {e}"}), 500

    if status != 201:
        app.logger.error(f"[MP] status={status} body={json.dumps(body)}")
        msg = body.get("message") or "no se pudo crear preferencia"
        return jsonify({"error": msg}), 400

    init_point = body.get("init_point") or body.get("sandbox_init_point")
    if not init_point:
        app.logger.error(f"[MP] Preferencia sin init_point: {json.dumps(body)}")
        return jsonify({"error": "MP no devolvió init_point"}), 400

    # Respondemos con link para que el front pinte el QR
    return jsonify({
        "pref_id": body.get("id"),
        "qr_link": init_point,
        "producto": p.to_dict()
    })


# ---------------------------
# Webhook Mercado Pago
# ---------------------------
@app.post("/webhook")
def webhook():
    """
    Recibe notificaciones de MP.
    Guarda registro 'pendiente' y payload crudo. Si viene id de pago,
    intenta actualizar estado e importe.
    """
    try:
        payload = request.get_json(silent=True) or {}
    except Exception:
        payload = {}

    app.logger.info(f"[WEBHOOK] headers={dict(request.headers)}")
    app.logger.info(f"[WEBHOOK] payload={json.dumps(payload)}")

    tipo = payload.get("type") or payload.get("type_id") or payload.get("action")  # MP puede variar
    data = payload.get("data") or {}
    payment_id = data.get("id") or payload.get("id")

    # Guardamos siempre el crudo
    pago = Pago(
        id_pago=str(payment_id) if payment_id else None,
        estado="pendiente",
        producto="(vía webhook)",
        slot_id=None,
        monto=None,
        raw=json.dumps(payload),
    )
    # idempotencia simple por id_pago
    if payment_id:
        exists = Pago.query.filter_by(id_pago=str(payment_id)).first()
        if not exists:
            db.session.add(pago)
            db.session.commit()
    else:
        db.session.add(pago)
        db.session.commit()

    # Si tenemos payment_id, intentamos consultar a MP
    if payment_id:
        sdk, err = get_mp_sdk()
        if not err:
            try:
                r = sdk.payment().get(payment_id)
                status = r.get("status")
                body = r.get("response", {}) if isinstance(r, dict) else {}
                estado = body.get("status")
                monto = body.get("transaction_amount")
                desc = (body.get("description") or "")[:160]
                # actualizar/insertar
                pago = Pago.query.filter_by(id_pago=str(payment_id)).first() or pago
                pago.estado = estado or pago.estado
                pago.monto = float(monto) if monto else pago.monto
                pago.producto = desc or pago.producto
                db.session.add(pago)
                db.session.commit()
            except Exception as e:
                app.logger.exception(f"[WEBHOOK] Error consultando MP payment {payment_id}: {e}")

    return jsonify({"ok": True})


# ---------------------------
# Run
# ---------------------------
if __name__ == "__main__":
    # Para local: FLASK_ENV=development
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
