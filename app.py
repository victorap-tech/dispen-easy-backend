# app.py
import os, io, json, base64, ssl
from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
import requests
import qrcode

# ---------------------------
# Flask / CORS
# ---------------------------
app = Flask(__name__)

# DB (Railway expone DATABASE_URL)
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "sqlite:///pagos.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

CORS(app, resources={r"/api/*": {
    "origins": [
        os.getenv("FRONT_URL", "*"),
        "http://localhost:3000",
        "https://dispen-easy-web-production.up.railway.app",
    ],
    "allow_headers": ["Content-Type", "Authorization"],
    "methods": ["GET", "POST", "DELETE", "OPTIONS"]
}})

# ---------------------------
# Modelos
# ---------------------------
class Pago(db.Model):
    id          = db.Column(db.Integer, primary_key=True)
    id_pago     = db.Column(db.String(120), unique=True, nullable=False)
    estado      = db.Column(db.String(40),  nullable=False)
    producto    = db.Column(db.String(120), nullable=True)
    slot_id     = db.Column(db.Integer, nullable=True)
    monto       = db.Column(db.Float,   nullable=True)
    raw         = db.Column(db.Text,    nullable=True)

class Producto(db.Model):
    id          = db.Column(db.Integer, primary_key=True)
    slot_id     = db.Column(db.Integer, unique=True, nullable=False)    # 1..6
    nombre      = db.Column(db.String(120), nullable=False, default="")
    precio      = db.Column(db.Float,  nullable=False, default=0.0)
    cantidad    = db.Column(db.Integer, nullable=False, default=1)
    habilitado  = db.Column(db.Boolean, nullable=False, default=False)
    created_at  = db.Column(db.DateTime, server_default=db.func.now())

    def to_dict(self):
        return {
            "id":         self.id,
            "slot_id":    self.slot_id,
            "nombre":     self.nombre,
            "precio":     self.precio,
            "cantidad":   self.cantidad,
            "habilitado": self.habilitado,
        }

# ---------------------------
# Crear tablas y asegurar esquema
# ---------------------------
def ensure_schema():
    """Agrega columnas faltantes si la tabla ya existía (especial para Postgres en Railway)."""
    from sqlalchemy import text
    dialect = db.engine.url.get_backend_name()

    # Siempre crear tablas si no existen
    db.create_all()

    if dialect.startswith("postgres"):
        # Agregar columnas si faltan (idempotente)
        stmts = [
            "ALTER TABLE producto ADD COLUMN IF NOT EXISTS slot_id INTEGER NOT NULL DEFAULT 0;",
            "ALTER TABLE producto ADD COLUMN IF NOT EXISTS nombre VARCHAR(120) NOT NULL DEFAULT '';",
            "ALTER TABLE producto ADD COLUMN IF NOT EXISTS precio DOUBLE PRECISION NOT NULL DEFAULT 0;",
            "ALTER TABLE producto ADD COLUMN IF NOT EXISTS cantidad INTEGER NOT NULL DEFAULT 1;",
            "ALTER TABLE producto ADD COLUMN IF NOT EXISTS habilitado BOOLEAN NOT NULL DEFAULT FALSE;",
            "ALTER TABLE producto ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW();",
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname='idx_producto_slot') THEN "
            "CREATE UNIQUE INDEX idx_producto_slot ON producto(slot_id); END IF; END $$;"
        ]
        for s in stmts:
            db.session.execute(text(s))
        db.session.commit()
    elif dialect.startswith("sqlite"):
        # Comprobar columnas presentes
        from sqlalchemy import text
        res = db.session.execute(text("PRAGMA table_info(producto)"))
        cols = {row[1] for row in res}
        # SQLite permite ADD COLUMN, siempre como nullable con default
        add = []
        if "slot_id" not in cols:    add.append("ALTER TABLE producto ADD COLUMN slot_id INTEGER NOT NULL DEFAULT 0;")
        if "nombre" not in cols:     add.append("ALTER TABLE producto ADD COLUMN nombre TEXT NOT NULL DEFAULT '';")
        if "precio" not in cols:     add.append("ALTER TABLE producto ADD COLUMN precio REAL NOT NULL DEFAULT 0;")
        if "cantidad" not in cols:   add.append("ALTER TABLE producto ADD COLUMN cantidad INTEGER NOT NULL DEFAULT 1;")
        if "habilitado" not in cols: add.append("ALTER TABLE producto ADD COLUMN habilitado INTEGER NOT NULL DEFAULT 0;")
        if "created_at" not in cols: add.append("ALTER TABLE producto ADD COLUMN created_at TEXT DEFAULT CURRENT_TIMESTAMP;")
        for s in add:
            db.session.execute(text(s))
        db.session.commit()

with app.app_context():
    ensure_schema()
    print("[DB] esquema OK", flush=True)

# ---------------------------
# Helpers
# ---------------------------
def fila_vacia(slot_id: int):
    return {"id": None, "slot_id": slot_id, "nombre": "", "precio": 0.0, "cantidad": 1, "habilitado": False}

# --- MQTT ---
import paho.mqtt.client as mqtt
def _mqtt_client():
    broker   = os.getenv("MQTT_BROKER")        # ej: c9b4a2bb-...s1.eu.hivemq.cloud
    port     = int(os.getenv("MQTT_PORT", "8883"))
    user     = os.getenv("MQTT_USER")
    pwd      = os.getenv("MQTT_PASS")
    client_id= os.getenv("MQTT_CLIENT_ID", "dispen-easy-backend")
    if not all([broker, user, pwd]):
        raise RuntimeError("Faltan variables MQTT_BROKER / MQTT_USER / MQTT_PASS")

    c = mqtt.Client(client_id=client_id, clean_session=True)
    c.username_pw_set(user, pwd)
    # TLS
    c.tls_set(tls_version=ssl.PROTOCOL_TLS, cert_reqs=ssl.CERT_REQUIRED)
    c.tls_insecure_set(False)
    return c, broker, port

def mqtt_publish(payload: dict, topic: str | None = None, qos: int = 1, retain: bool = False):
    topic = topic or os.getenv("MQTT_TOPIC", "dispen-easy/dispensar")
    client, broker, port = _mqtt_client()
    client.connect(broker, port, keepalive=int(os.getenv("MQTT_KEEPALIVE", "60")))
    client.loop_start()
    try:
        info = client.publish(topic, json.dumps(payload), qos=qos, retain=retain)
        info.wait_for_publish(timeout=5)
    finally:
        client.loop_stop()
        client.disconnect()

# --- MercadoPago REST helpers ---
def mp_headers():
    token = os.getenv("MP_ACCESS_TOKEN")
    if not token:
        raise RuntimeError("Falta MP_ACCESS_TOKEN")
    return {"Authorization": f"Bearer {token}"}

def mp_create_preference(producto: Producto):
    """Crea una preferencia y devuelve (init_point, preference_id, raw_json)."""
    url = "https://api.mercadopago.com/checkout/preferences"
    payload = {
        "items": [{
            "title": producto.nombre,
            "quantity": 1,
            "currency_id": os.getenv("MP_CURRENCY", "ARS"),
            "unit_price": float(producto.precio)
        }],
        "description": producto.nombre,
        "metadata": {
            "producto_id": producto.id,
            "slot_id": producto.slot_id
        },
        "notification_url": os.getenv("WEBHOOK_URL") or os.getenv("PUBLIC_URL", "") + "/webhook",
        "back_urls": {
            "success": os.getenv("FRONT_URL", ""),
            "pending": os.getenv("FRONT_URL", ""),
            "failure": os.getenv("FRONT_URL", "")
        },
        "auto_return": "approved",
        "external_reference": f"prod:{producto.id}"
    }
    r = requests.post(url, headers=mp_headers(), json=payload, timeout=12)
    if r.status_code != 201:
        raise RuntimeError(f"MP error {r.status_code}: {r.text}")
    data = r.json()
    return data.get("init_point"), data.get("id"), data

def mp_get_payment(payment_id: str):
    url = f"https://api.mercadopago.com/v1/payments/{payment_id}"
    r = requests.get(url, headers=mp_headers(), timeout=12)
    if r.status_code != 200:
        raise RuntimeError(f"MP payment {r.status_code}: {r.text}")
    return r.json()

def mp_get_merchant_order_payid(mo_id: str) -> str | None:
    url = f"https://api.mercadolibre.com/merchant_orders/{mo_id}"
    r = requests.get(url, headers=mp_headers(), timeout=12)
    if r.status_code != 200:
        return None
    mo = r.json()
    pays = mo.get("payments", []) or []
    return str(pays[-1].get("id")) if pays else None

# ---------------------------
# Rutas
# ---------------------------
@app.route("/")
def root():
    return jsonify({"mensaje": "API de Dispen-Easy funcionando"})

# ---- CRUD (6 slots fijos) ----
@app.route("/api/productos", methods=["GET"])
def listar_productos():
    slots = {i: fila_vacia(i) for i in range(1, 7)}
    for p in Producto.query.order_by(Producto.slot_id.asc()).all():
        slots[p.slot_id] = p.to_dict()
    return jsonify([slots[i] for i in range(1, 7)])

@app.route("/api/productos", methods=["POST"])
def crear_o_actualizar_producto():
    data = request.get_json(force=True) or {}
    print("[POST /api/productos] body:", data, flush=True)

    try:
        slot_id   = int(data.get("slot_id", 0) or 0)
        nombre    = (data.get("nombre") or "").strip()
        precio    = float(data.get("precio", 0) or 0)
        cantidad  = int(data.get("cantidad", 1) or 1)
        habilitado= bool(data.get("habilitado", False))
    except Exception as e:
        print("[POST /api/productos] parse error:", e, flush=True)
        return jsonify({"error": "payload inválido"}), 400

    if slot_id not in range(1, 7):
        return jsonify({"error": "slot_id inválido (1..6)"}), 400

    try:
        p = Producto.query.filter_by(slot_id=slot_id).first()
        created = False
        if not p:
            p = Producto(slot_id=slot_id)
            db.session.add(p)
            created = True

        p.nombre = nombre
        p.precio = precio
        p.cantidad = cantidad
        p.habilitado = habilitado

        db.session.commit()
        print(f"[POST /api/productos] OK slot={slot_id} {'(new)' if created else '(upd)'}", flush=True)
        return jsonify(p.to_dict())
    except Exception as e:
        db.session.rollback()
        print("[POST /api/productos] DB error:", e, flush=True)
        return jsonify({"error": "DB error"}), 500

@app.route("/api/productos/<int:slot_id>", methods=["DELETE"])
def eliminar_por_slot(slot_id):
    p = Producto.query.filter_by(slot_id=slot_id).first()
    if not p:
        return jsonify({"mensaje": "vacío"}), 200
    db.session.delete(p)
    db.session.commit()
    return jsonify({"mensaje": "eliminado"})

# ---- Generar QR (MercadoPago) ----
@app.route("/api/generar_qr/<int:slot_id>", methods=["GET"])
def generar_qr(slot_id):
    p = Producto.query.filter_by(slot_id=slot_id).first()
    if not p or not p.habilitado or not p.nombre or p.precio <= 0:
        return jsonify({"error": "Producto no válido (habilitado/nombre/precio)"}), 400
    try:
        link, pref_id, raw = mp_create_preference(p)
    except Exception as e:
        print("MP preferencia error:", e, flush=True)
        return jsonify({"error": "No se pudo generar link de pago"}), 502

    # QR PNG -> base64
    buf = io.BytesIO()
    qrcode.make(link).save(buf, format="PNG")
    qr_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
    return jsonify({"qr_base64": qr_b64, "link": link, "preference_id": pref_id})

# ---- Webhook MercadoPago ----
@app.route("/webhook", methods=["POST"])
def webhook():
    raw = request.get_json(silent=True) or {}
    print("[webhook] raw:", raw, flush=True)

    # Resolver payment_id
    payment_id = None
    if isinstance(raw.get("data"), dict) and raw["data"].get("id"):
        payment_id = str(raw["data"]["id"])

    if not payment_id and raw.get("topic") == "payment" and raw.get("resource"):
        # /v1/payments/{id}
        try:
            payment_id = raw["resource"].rstrip("/").split("/")[-1]
        except Exception:
            payment_id = None

    if not payment_id and raw.get("topic") == "merchant_order" and raw.get("resource"):
        mo_id = raw["resource"].rstrip("/").split("/")[-1]
        payment_id = mp_get_merchant_order_payid(mo_id)

    if not payment_id:
        print("[webhook] sin payment_id -> ignored", flush=True)
        return jsonify({"status": "ignored"}), 200

    try:
        pay = mp_get_payment(payment_id)
    except Exception as e:
        print("[webhook] error consultando payment:", e, flush=True)
        return jsonify({"status": "ok"}), 200

    estado = (pay.get("status") or "").lower()
    meta   = pay.get("metadata") or {}
    slot   = meta.get("slot_id")
    prod_id= meta.get("producto_id")
    monto  = pay.get("transaction_amount")

    # Guardar/actualizar pago
    try:
        reg = Pago.query.filter_by(id_pago=str(pay.get("id"))).first()
        if not reg:
            reg = Pago(id_pago=str(pay.get("id")))
            db.session.add(reg)
        reg.estado   = estado
        reg.producto = str(prod_id)
        reg.slot_id  = int(slot) if slot is not None else None
        reg.monto    = float(monto) if monto is not None else None
        reg.raw      = json.dumps(pay)
        db.session.commit()
    except Exception as e:
        print("[DB] error guardando pago:", e, flush=True)
        db.session.rollback()

    # Dispensar si está aprobado y tenemos slot
    if estado == "approved" and slot:
        try:
            mqtt_publish({
                "ok": 1,
                "evento": "dispensar",
                "slot_id": int(slot),
                "pago_id": str(pay.get("id")),
                "monto": monto
            })
            print(f"[MQTT] enviado -> slot {slot}", flush=True)
        except Exception as e:
            print("[MQTT] error:", e, flush=True)

    return jsonify({"status": "ok"}), 200

# ---------------------------
# Main (para correr local)
# ---------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
