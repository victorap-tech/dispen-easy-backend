# app.py
import os, json, base64, io, datetime as dt
from flask import Flask, jsonify, request, send_file
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text, UniqueConstraint
from flask_cors import CORS

# ---------- Mercado Pago SDK ----------
try:
    import mercadopago
except Exception:
    mercadopago = None

MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "").strip() or None
sdk = mercadopago.SDK(MP_ACCESS_TOKEN) if (mercadopago and MP_ACCESS_TOKEN) else None

# ---------- Flask / DB ----------
app = Flask(__name__)
CORS(app)

db_url = os.environ.get("DATABASE_URL")
# Compatibilidad con postgres://
if db_url and db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

# ---------- Modelos ----------
class Producto(db.Model):
    __tablename__ = "producto"
    id          = db.Column(db.Integer, primary_key=True, index=True)
    slot_id     = db.Column(db.Integer, nullable=False)
    nombre      = db.Column(db.String(120), nullable=False, default="")
    precio      = db.Column(db.Float, nullable=False, default=0.0)   # en ARS
    cantidad    = db.Column(db.Integer, nullable=False, default=1)   # litros
    habilitado  = db.Column(db.Boolean, nullable=False, default=False)
    created_at  = db.Column(db.DateTime, nullable=False, default=dt.datetime.utcnow)

    __table_args__ = (UniqueConstraint("slot_id", name="idx_producto_slot"),)

class Pago(db.Model):
    __tablename__ = "pago"
    id          = db.Column(db.Integer, primary_key=True)
    id_pago     = db.Column(db.String(64), nullable=False, unique=True, index=True)
    estado      = db.Column(db.String(32), nullable=False, default="pendiente")
    producto    = db.Column(db.String(120), nullable=True)
    slot_id     = db.Column(db.Integer, nullable=True)
    monto       = db.Column(db.Float, nullable=True)
    raw         = db.Column(db.Text, nullable=True)
    dispensado  = db.Column(db.Boolean, nullable=False, default=False)
    created_at  = db.Column(db.DateTime, nullable=False, default=dt.datetime.utcnow)

# ---------- Utils ----------
def ensure_schema():
    """
    Crea tablas y agrega columnas/índices que pudieran faltar (idempotente).
    Railway a veces arranca la DB sin migraciones; esto lo deja en orden.
    """
    db.create_all()  # crea tablas si no existen

    # Ajustes idempotentes específicos para Postgres
    try:
        dialect = db.engine.url.get_backend_name()
        if dialect.startswith("postgres"):
            stmts = [
                # asegurar columnas (por si vienen de una versión previa)
                "ALTER TABLE producto ADD COLUMN IF NOT EXISTS slot_id INTEGER NOT NULL DEFAULT 0;",
                "ALTER TABLE producto ADD COLUMN IF NOT EXISTS nombre VARCHAR(120) NOT NULL DEFAULT '';",
                "ALTER TABLE producto ADD COLUMN IF NOT EXISTS precio DOUBLE PRECISION NOT NULL DEFAULT 0;",
                "ALTER TABLE producto ADD COLUMN IF NOT EXISTS cantidad INTEGER NOT NULL DEFAULT 1;",
                "ALTER TABLE producto ADD COLUMN IF NOT EXISTS habilitado BOOLEAN NOT NULL DEFAULT FALSE;",
                "ALTER TABLE producto ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP;",
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname='idx_producto_slot') THEN "
                "CREATE UNIQUE INDEX idx_producto_slot ON producto(slot_id); END IF; END $$;",

                "ALTER TABLE pago ADD COLUMN IF NOT EXISTS id_pago VARCHAR(64);",
                "ALTER TABLE pago ADD COLUMN IF NOT EXISTS estado VARCHAR(32) DEFAULT 'pendiente';",
                "ALTER TABLE pago ADD COLUMN IF NOT EXISTS producto VARCHAR(120);",
                "ALTER TABLE pago ADD COLUMN IF NOT EXISTS slot_id INTEGER;",
                "ALTER TABLE pago ADD COLUMN IF NOT EXISTS monto DOUBLE PRECISION;",
                "ALTER TABLE pago ADD COLUMN IF NOT EXISTS raw TEXT;",
                "ALTER TABLE pago ADD COLUMN IF NOT EXISTS dispensado BOOLEAN DEFAULT FALSE;",
                "ALTER TABLE pago ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP;",
                "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname='idx_pago_id_pago') THEN "
                "CREATE UNIQUE INDEX idx_pago_id_pago ON pago(id_pago); END IF; END $$;",
            ]
            with db.engine.begin() as conn:
                for s in stmts:
                    conn.execute(text(s))
        print("[DB] esquema OK", flush=True)
    except Exception as e:
        print("[DB] error ensure_schema:", e, flush=True)

def base_url():
    # https://web-production-xxxx.up.railway.app
    # request.host_url ya trae https en Railway
    return request.host_url.rstrip("/")

def make_qr_png_base64(texto: str) -> str:
    import qrcode
    from PIL import Image
    img = qrcode.make(texto)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{b64}"

# ---------- Rutas ----------
@app.route("/")
def root():
    return jsonify({"ok": True, "message": "Dispen-Easy backend activo"})

@app.get("/api/productos")
def listar_productos():
    # siempre devolvemos slots 1..6 para el panel
    slots = {p.slot_id: p for p in Producto.query.all()}
    data = []
    for sid in range(1, 7):
        p = slots.get(sid)
        data.append({
            "slot_id": sid,
            "nombre": p.nombre if p else "",
            "precio": p.precio if p else 0,
            "cantidad": p.cantidad if p else 1,
            "habilitado": p.habilitado if p else False,
        })
    return jsonify(data)

@app.post("/api/productos/<int:slot_id>")
def upsert_producto(slot_id: int):
    body = request.get_json(silent=True) or {}
    nombre     = (body.get("nombre") or "").strip()
    precio     = float(body.get("precio") or 0)
    cantidad   = int(body.get("cantidad") or 1)
    habilitado = bool(body.get("habilitado"))

    p = Producto.query.filter_by(slot_id=slot_id).first()
    if not p:
        p = Producto(slot_id=slot_id)
        db.session.add(p)

    p.nombre = nombre
    p.precio = precio
    p.cantidad = cantidad
    p.habilitado = habilitado
    db.session.commit()

    return jsonify({"ok": True})

@app.delete("/api/productos/<int:slot_id>")
def delete_producto(slot_id: int):
    p = Producto.query.filter_by(slot_id=slot_id).first()
    if not p:
        return jsonify({"ok": True})  # idempotente
    db.session.delete(p)
    db.session.commit()
    return jsonify({"ok": True})

@app.get("/api/generar_qr/<int:slot_id>")
def generar_qr(slot_id: int):
    if sdk is None:
        return jsonify({"error": "MP_ACCESS_TOKEN no configurado"}), 500

    p = Producto.query.filter_by(slot_id=slot_id).first()
    if not p or not p.habilitado or not p.nombre or (p.precio or 0) <= 0:
        return jsonify({"error": "Producto no habilitado o sin nombre/precio"}), 400

    # Armar preferencia Checkout Pro
pref_data = {
    "items": [{
        "title": p.nombre,
        "quantity": 1,
        "unit_price": float(p.precio)
    }],
    "metadata": {"producto_id": p.id, "slot_id": p.slot_id},
    "external_reference": f"prod:{p.id}",
    "back_urls": {
        "success": request.host_url.rstrip("/") + "/pago_exitoso",
        "pending": request.host_url.rstrip("/") + "/pago_pendiente",
        "failure": request.host_url.rstrip("/") + "/pago_fallido",
    },
    "notification_url": request.host_url.rstrip("/") + "/webhook",
    "auto_return": "approved",
    "currency_id": "ARS"
}

    pref = sdk.preference().create(pref_data)
    if pref.get("status") != 201:
        detalle = pref.get("response")
        print("[MP] error pref:", pref.get("status"), detalle, flush=True)
        return jsonify({"error": "No se pudo crear preferencia", "detalle": detalle}), 502

    init_point = pref["response"].get("init_point") or pref["response"].get("sandbox_init_point")
    qr_png = make_qr_png_base64(init_point)

    return jsonify({
        "ok": True,
        "url": init_point,
        "qr_png": qr_png,
        "producto": {"nombre": p.nombre, "precio": p.precio, "slot_id": p.slot_id},
    })

# Webhook compatible GET/POST
@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    try:
        if request.method == "POST":
            body = request.get_json(silent=True) or {}
            topic = body.get("type") or body.get("topic")
            id_ref = (body.get("data") or {}).get("id")
        else:
            topic = request.args.get("topic") or request.args.get("type")
            id_ref = request.args.get("id") or request.args.get("data.id")

        print("[webhook] method=", request.method, "topic=", topic, "id=", id_ref, flush=True)

        if not topic or not id_ref:
            return jsonify({"ok": True}), 200

        if topic in ("payment", "payments"):
            if sdk is None:
                return jsonify({"ok": True}), 200

            pago = sdk.payment().get(id_ref)
            status_code = pago.get("status")
            data = pago.get("response", {}) if isinstance(pago, dict) else {}
            print("[webhook] pago.status", status_code, "pago.id", id_ref, flush=True)

            if status_code == 200:
                estado = data.get("status") or "pendiente"
                metadata = data.get("metadata") or {}
                producto_id = metadata.get("producto_id")
                slot_id = metadata.get("slot_id")
                monto = data.get("transaction_amount")
                try:
                    sql = """
                        INSERT INTO pago (id_pago, estado, producto, slot_id, monto, raw, dispensado)
                        VALUES (:id_pago, :estado, :producto, :slot_id, :monto, :raw, FALSE)
                        ON CONFLICT (id_pago) DO UPDATE
                        SET estado=EXCLUDED.estado,
                            producto=EXCLUDED.producto,
                            slot_id=EXCLUDED.slot_id,
                            monto=EXCLUDED.monto,
                            raw=EXCLUDED.raw;
                    """
                    params = {
                        "id_pago": str(id_ref),
                        "estado": estado,
                        "producto": str(producto_id) if producto_id is not None else None,
                        "slot_id": slot_id,
                        "monto": float(monto) if monto is not None else None,
                        "raw": json.dumps(data),
                    }
                    db.session.execute(text(sql), params)
                    db.session.commit()
                except Exception as e:
                    db.session.rollback()
                    print("[DB] error guardando pago:", e, flush=True)

        return jsonify({"ok": True}), 200

    except Exception as e:
        print("[webhook] exception:", e, flush=True)
        return jsonify({"ok": True}), 200

# ---------- Inicio ----------
with app.app_context():
    ensure_schema()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
