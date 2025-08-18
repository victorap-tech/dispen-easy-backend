import os
import json
from datetime import datetime

from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS

# --- Mercado Pago SDK ---
import mercadopago

# ------------------ Config ------------------
DATABASE_URL = os.environ.get("DATABASE_URL")
MP_ACCESS_TOKEN = os.environ.get("MP_ACCESS_TOKEN")  # De tu cuenta vendedora
BASE_URL = os.environ.get("BASE_URL")  # opcional, ej: "https://web-production-e7d2.up.railway.app"

app = Flask(__name__)
CORS(app)

# Railway ya trae DATABASE_URL (Postgres)
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL no configurada")

app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

# --------------- Modelos --------------------
class Producto(db.Model):
    __tablename__ = "producto"
    id         = db.Column(db.Integer, primary_key=True)
    slot_id    = db.Column(db.Integer, nullable=False, index=True, unique=True)
    nombre     = db.Column(db.String(120), nullable=False, default="")
    precio     = db.Column(db.Float, nullable=False, default=0.0)  # ARS
    cantidad   = db.Column(db.Integer, nullable=False, default=1)
    habilitado = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def to_dict(self):
        return dict(
            id=self.id,
            slot_id=self.slot_id,
            nombre=self.nombre,
            precio=self.precio,
            cantidad=self.cantidad,
            habilitado=self.habilitado,
            created_at=self.created_at.isoformat() + "Z",
        )


class Pago(db.Model):
    __tablename__ = "pago"
    id         = db.Column(db.Integer, primary_key=True)
    id_pago    = db.Column(db.String(64), nullable=False, unique=True)  # payment_id / TEST...
    estado     = db.Column(db.String(32), nullable=False, default="pendiente")
    producto   = db.Column(db.String(160), nullable=True)
    slot_id    = db.Column(db.Integer, nullable=True)
    monto      = db.Column(db.Float, nullable=True)
    raw        = db.Column(db.Text, nullable=True)  # JSON string
    dispensado = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

# --------------- Esquema / auto-migración --------------------
def ensure_schema():
    """Crea tablas y agrega columnas/filtros/fks si faltan (idempotente)."""
    from sqlalchemy import text
    db.create_all()  # crea si no existen

    dialect = db.engine.url.get_backend_name()
    if dialect.startswith("postgres"):
        stmts = [
            # producto
            "ALTER TABLE producto ADD COLUMN IF NOT EXISTS slot_id INTEGER NOT NULL DEFAULT 0;",
            "ALTER TABLE producto ADD COLUMN IF NOT EXISTS nombre VARCHAR(120) NOT NULL DEFAULT '';",
            "ALTER TABLE producto ADD COLUMN IF NOT EXISTS precio DOUBLE PRECISION NOT NULL DEFAULT 0;",
            "ALTER TABLE producto ADD COLUMN IF NOT EXISTS cantidad INTEGER NOT NULL DEFAULT 1;",
            "ALTER TABLE producto ADD COLUMN IF NOT EXISTS habilitado BOOLEAN NOT NULL DEFAULT FALSE;",
            "ALTER TABLE producto ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW();",
            # índice único por slot
            """
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname='idx_producto_slot_unique') THEN
                    CREATE UNIQUE INDEX idx_producto_slot_unique ON producto(slot_id);
                END IF;
            END $$;
            """,
            # pago
            "CREATE TABLE IF NOT EXISTS pago (id SERIAL PRIMARY KEY);",
            "ALTER TABLE pago ADD COLUMN IF NOT EXISTS id_pago VARCHAR(64);",
            "ALTER TABLE pago ADD COLUMN IF NOT EXISTS estado VARCHAR(32) DEFAULT 'pendiente';",
            "ALTER TABLE pago ADD COLUMN IF NOT EXISTS producto VARCHAR(160);",
            "ALTER TABLE pago ADD COLUMN IF NOT EXISTS slot_id INTEGER;",
            "ALTER TABLE pago ADD COLUMN IF NOT EXISTS monto DOUBLE PRECISION;",
            "ALTER TABLE pago ADD COLUMN IF NOT EXISTS raw TEXT;",
            "ALTER TABLE pago ADD COLUMN IF NOT EXISTS dispensado BOOLEAN DEFAULT FALSE;",
            "ALTER TABLE pago ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW();",
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='pago_id_pago_key') THEN ALTER TABLE pago ADD CONSTRAINT pago_id_pago_key UNIQUE(id_pago); END IF; END $$;",
        ]
        with db.engine.begin() as conn:
            for s in stmts:
                conn.execute(text(s))

ensure_schema()

# --------------- MP SDK helper --------------------
def get_mp():
    if not MP_ACCESS_TOKEN:
        return None
    try:
        sdk = mercadopago.SDK(MP_ACCESS_TOKEN)
        return sdk
    except Exception:
        return None

def absolute_base():
    if BASE_URL:
        return BASE_URL.rstrip("/")
    # arma desde la request actual
    # ej: https://web-production-xxxx.up.railway.app
    return request.host_url.rstrip("/")

# --------------- Rutas básicas --------------------
@app.get("/")
def root():
    return "<h1>Dispen-Easy API</h1>"

@app.get("/ok")
def ok(): return "<h1>OK</h1>"

@app.get("/pend")
def pend(): return "<h1>PEND</h1>"

@app.get("/fail")
def fail(): return "<h1>FAIL</h1>"

# --------------- Productos CRUD --------------------
@app.get("/api/productos")
def get_productos():
    prods = Producto.query.order_by(Producto.slot_id.asc()).all()
    return jsonify([p.to_dict() for p in prods])

@app.post("/api/productos/<int:slot_id>")
def upsert_producto(slot_id: int):
    data = request.get_json(silent=True) or {}
    p = Producto.query.filter_by(slot_id=slot_id).first()
    if not p:
        p = Producto(slot_id=slot_id)
        db.session.add(p)
    p.nombre     = str(data.get("nombre", "")).strip()
    p.precio     = float(data.get("precio", 0) or 0)
    p.cantidad   = int(data.get("cantidad", 1) or 1)
    p.habilitado = bool(data.get("habilitado", False))
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

# --------------- Generar QR (preferencia) --------------------
@app.route("/api/generar_qr/<int:slot_id>", methods=["GET", "POST"])
def generar_qr(slot_id: int):
    sdk = get_mp()
    if sdk is None:
        return jsonify({"error": "MP_ACCESS_TOKEN no configurado"}), 500

    p = Producto.query.filter_by(slot_id=slot_id).first()
    if (not p) or (not p.habilitado) or (not p.nombre) or (p.precio is None) or (float(p.precio) <= 0):
        return jsonify({"error": "Producto inválido (habilitado/nombre/precio)"}), 400

    base = absolute_base()
    pref_data = {
        "items": [
            {
                "title": p.nombre or f"Producto {p.slot_id}",
                "quantity": 1,
                "unit_price": float(p.precio),
                "currency_id": "ARS",
                "description": p.nombre or "",
            }
        ],
        "back_urls": {
            "success": f"{base}/ok",
            "pending": f"{base}/pend",
            "failure": f"{base}/fail",
        },
        "auto_return": "approved",
        "notification_url": f"{base}/webhook",
        "external_reference": f"prod:{p.id}",
        "metadata": {"producto_id": p.id, "slot_id": p.slot_id},
    }

    try:
        pref = sdk.preference().create(pref_data)
        status = pref.get("status")
        resp   = pref.get("response", {}) or {}
        if status != 201:
            # burbujeamos error real
            return jsonify({
                "error": "no se pudo crear preferencia",
                "detalle": {"status": status, **resp}
            }), 400

        init_point = resp.get("init_point") or resp.get("sandbox_init_point")
        if not init_point:
            return jsonify({"error": "MP no devolvió init_point", "detalle": resp}), 400

        # opcional: pre-registrar un pago "pendiente" sin id_pago
        return jsonify({"ok": True, "init_point": init_point})
    except Exception as e:
        return jsonify({"error": "excepción creando preferencia", "detalle": str(e)}), 500

# --------------- Webhook --------------------
@app.post("/webhook")
def webhook():
    """
    Acepta el JSON de MP. Cuando llega un payment, consulta el detalle
    y upsertea en la tabla 'pago'.
    """
    body = request.get_json(silent=True) or {}
    # Soportar simulador que manda otras formas
    type_ = body.get("type") or body.get("type_id") or ""
    data  = body.get("data") or {}
    payment_id = str(data.get("id") or "").strip()

    # Algunas integraciones antiguas mandan query-string ?data.id=123
    if not payment_id:
        payment_id = request.args.get("data.id", "")

    if type_ != "payment" or not payment_id:
        # aceptar silenciosamente para no reintentar indefinido
        return jsonify({"ok": True, "skip": True})

    sdk = get_mp()
    if sdk is None:
        return jsonify({"ok": False, "error": "MP_ACCESS_TOKEN ausente"}), 500

    try:
        pay = sdk.payment().get(payment_id)
        p_status = pay.get("status")
        presp    = pay.get("response", {}) or {}

        estado = presp.get("status") or "pendiente"
        monto  = presp.get("transaction_amount")
        desc   = (presp.get("description") or "")[:160]
        metadata = presp.get("metadata") or {}
        slot_id = metadata.get("slot_id")

        # UPSERT manual
        row = Pago.query.filter_by(id_pago=str(payment_id)).first()
        if not row:
            row = Pago(id_pago=str(payment_id))
            db.session.add(row)
        row.estado   = estado
        row.monto    = float(monto) if monto is not None else None
        row.producto = desc
        row.slot_id  = int(slot_id) if slot_id is not None else None
        row.raw      = json.dumps(presp, ensure_ascii=False)
        db.session.commit()

        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ------- util para probar webhook rápido desde Postman/curl ------
@app.get("/webhook")
def webhook_info():
    return "Method Not Allowed (use POST)", 405

# --------------- Main --------------------
if __name__ == "__main__":
    # Para correr local: FLASK_ENV=development python app.py
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
