import os
import json
from datetime import datetime

from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS

# ---------------------------
# App & DB
# ---------------------------
app = Flask(__name__)
CORS(app)

db_url = os.environ.get("DATABASE_URL")
if db_url and db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = db_url or "sqlite:///local.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

# ---------------------------
# Modelos
# ---------------------------
class Producto(db.Model):
    __tablename__ = "producto"
    id          = db.Column(db.Integer, primary_key=True, index=True)
    slot_id     = db.Column(db.Integer, nullable=False, index=True)
    nombre      = db.Column(db.String(120), nullable=False, default="")
    precio      = db.Column(db.Float, nullable=False, default=0.0)
    cantidad    = db.Column(db.Integer, nullable=False, default=1)
    habilitado  = db.Column(db.Boolean, nullable=False, default=False)
    created_at  = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "slot_id": self.slot_id,
            "nombre": self.nombre,
            "precio": self.precio,
            "cantidad": self.cantidad,
            "habilitado": self.habilitado,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

class Pago(db.Model):
    __tablename__ = "pago"
    id = db.Column(db.Integer, primary_key=True)
    id_pago = db.Column(db.String(64), unique=True, index=True)
    estado = db.Column(db.String(32))
    producto = db.Column(db.String(160))
    slot_id = db.Column(db.Integer)
    monto = db.Column(db.Float)
    raw = db.Column(db.Text)
    dispensado = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

# ---------------------------
# Crear/ajustar esquema (idempotente)
# ---------------------------
def ensure_schema():
    db.create_all()

    # En Railway a veces existen tablas sin todas las columnas; las agregamos idempotentemente.
    from sqlalchemy import text
    dialect = db.engine.url.get_backend_name()
    if dialect.startswith("postgres"):
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
            "ALTER TABLE pago ADD COLUMN IF NOT EXISTS estado VARCHAR(32);",
            "ALTER TABLE pago ADD COLUMN IF NOT EXISTS producto VARCHAR(160);",
            "ALTER TABLE pago ADD COLUMN IF NOT EXISTS slot_id INTEGER;",
            "ALTER TABLE pago ADD COLUMN IF NOT EXISTS monto DOUBLE PRECISION;",
            "ALTER TABLE pago ADD COLUMN IF NOT EXISTS raw TEXT;",
            "ALTER TABLE pago ADD COLUMN IF NOT EXISTS dispensado BOOLEAN DEFAULT FALSE;",
            "ALTER TABLE pago ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now();",
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname='idx_pago_id_pago_key') THEN "
            "CREATE UNIQUE INDEX idx_pago_id_pago_key ON pago(id_pago); END IF; END $$;",
        ]
        with db.engine.begin() as conn:
            for s in stmts:
                conn.execute(text(s))

with app.app_context():
    ensure_schema()

# ---------------------------
# Utilidades
# ---------------------------
def base_url():
    # Ej: "https://web-production-xxxx.up.railway.app/"
    return request.host_url

def get_mp_sdk():
    access = os.environ.get("MP_ACCESS_TOKEN")
    if not access:
        return None, "MP_ACCESS_TOKEN no configurado"
    try:
        import mercadopago
        sdk = mercadopago.SDK(access)
        return sdk, None
    except Exception as e:
        return None, f"Error importando SDK MercadoPago: {e}"

# ---------------------------
# Rutas básicas
# ---------------------------
@app.route("/", methods=["GET"])
def root():
    return "OK", 200

@app.route("/api/productos", methods=["GET"])
def listar_productos():
    filas = Producto.query.order_by(Producto.slot_id.asc()).all()
    return jsonify([p.to_dict() for p in filas])

@app.route("/api/productos/<int:slot_id>", methods=["POST"])
def upsert_producto(slot_id):
    data = request.get_json(silent=True) or request.form.to_dict()
    nombre     = (data.get("nombre") or "").strip()
    precio     = float(data.get("precio") or 0)
    cantidad   = int(data.get("cantidad") or 1)
    habilitado = str(data.get("habilitado") or "").lower() in ("1","true","on","si","sí")

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
def delete_producto(slot_id):
    p = Producto.query.filter_by(slot_id=slot_id).first()
    if not p:
        return jsonify({"ok": True})  # idempotente
    db.session.delete(p)
    db.session.commit()
    return jsonify({"ok": True})

# ---------------------------
# Generar QR / Preferencia MP
# ---------------------------
@app.route("/api/generar_qr/<int:slot_id>", methods=["POST", "GET"])
def generar_qr(slot_id):
    """
    Acepta POST desde el frontend. GET sólo para pruebas rápidas.
    """
    sdk, err = get_mp_sdk()
    if err:
        print(f"[MP] {err}")
        return jsonify({"error": err}), 500

    p = Producto.query.filter_by(slot_id=slot_id).first()
    print(f"[QR] Buscado slot_id={slot_id} -> {p.to_dict() if p else None}")

    if not p or not p.habilitado or not p.nombre or p.precio <= 0:
        return jsonify({"error": "no se pudo crear preferencia (¿producto habilitado y con nombre/precio?)"}), 400

    # URLs
    b = base_url().rstrip("/")  # sin barra final
    back_urls = {
        "success": f"{b}/",
        "pending": f"{b}/",
        "failure": f"{b}/",
    }
    notification_url = f"{b}/webhook"

    pref = {
        "items": [{
            "title": p.nombre,
            "quantity": 1,
            "currency_id": "ARS",
            "unit_price": float(p.precio)
        }],
        "external_reference": f"prod:{p.id}",
        "back_urls": back_urls,
        "auto_return": "approved",
        "notification_url": notification_url,
        "metadata": {"producto_id": p.id, "slot_id": p.slot_id},
    }

    print(f"[MP] Creando preferencia -> {json.dumps(pref, ensure_ascii=False)}")

    try:
        resp = sdk.preference().create(pref)
        status = resp.get("status")
        body = resp.get("response", {})
        print(f"[MP] status={status} response={json.dumps(body)[:500]}")

        if status != 201:
            return jsonify({"error": "No se pudo crear preferencia", "detalle": body}), 400

        # init_point (prod) o sandbox_init_point (test)
        return jsonify({
            "init_point": body.get("init_point"),
            "sandbox_init_point": body.get("sandbox_init_point"),
            "id_preference": body.get("id"),
        }), 200

    except Exception as e:
        print(f"[MP] Excepción creando preferencia: {e}")
        return jsonify({"error": str(e)}), 500

# ---------------------------
# Webhook Mercado Pago
# ---------------------------
@app.route("/webhook", methods=["POST"])
def webhook():
    # Aceptar JSON o form-encoded (MP a veces envía así)
    payload = request.get_json(silent=True)
    if payload is None:
        # si viene como form, lo intentamos decodificar
        try:
            payload = request.form.to_dict()
        except Exception:
            payload = {}

    print(f"[WEBHOOK] raw_in={payload}")

    # Guardar un registro mínimo para trazabilidad
    try:
        id_mp = None
        estado = None
        monto = None
        nombre_prod = None
        slot_id = None

        # Caso notificación 'merchant_order' o 'payment'
        topic = (payload.get("type") or payload.get("topic") or "").lower()

        # Si viene 'data': {'id': '...'}
        data_id = None
        if isinstance(payload.get("data"), dict):
            data_id = payload["data"].get("id")

        # Guardamos lo que haya (aunque sea test)
        registro = Pago(
            id_pago = str(data_id or payload.get("id") or "TEST_PAYMENT_123"),
            estado = estado or (payload.get("action") or topic),
            producto = nombre_prod or "Producto desconocido",
            slot_id = slot_id,
            monto = monto,
            raw = json.dumps(payload, ensure_ascii=False),
            dispensado = False
        )
        db.session.add(registro)
        db.session.commit()
        print(f"[WEBHOOK] guardado pago id={registro.id_pago}")

    except Exception as e:
        db.session.rollback()
        print(f"[WEBHOOK][DB] error guardando: {e}")

    # Responder 200 siempre (MP lo exige)
    return jsonify({"ok": True}), 200

# ---------------------------
# Entry point
# ---------------------------
if __name__ == "__main__":
    # Para correr local: FLASK_APP=app.py flask run
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
