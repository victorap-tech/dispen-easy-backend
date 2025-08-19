# app.py
import os
import json
import base64
import io
from decimal import Decimal
from datetime import datetime

from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS

# Mercado Pago SDK
try:
    from mercadopago import SDK as MP_SDK
except Exception:
    MP_SDK = None

# MQTT (opcional)
try:
    import paho.mqtt.client as mqtt
except Exception:
    mqtt = None

# QR
import qrcode


# ----------------------------
# Config Flask / SQLAlchemy
# ----------------------------
app = Flask(__name__)
CORS(app)

# Arregla postgres:// -> postgresql:// si hiciera falta
db_url = os.getenv("DATABASE_URL", "sqlite:///local.db")
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)


# ----------------------------
# Modelos
# ----------------------------
class Producto(db.Model):
    __tablename__ = "producto"

    id = db.Column(db.Integer, primary_key=True)
    slot_id = db.Column(db.Integer, index=True, unique=True, nullable=False, default=0)
    nombre = db.Column(db.String(120), nullable=False, default="")
    precio = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    cantidad = db.Column(db.Integer, nullable=False, default=1)  # litros, unidades, etc.
    habilitado = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class Pago(db.Model):
    __tablename__ = "pago"

    id = db.Column(db.Integer, primary_key=True)
    id_pago = db.Column(db.String(60), unique=True, index=True)  # id de MP
    estado = db.Column(db.String(40), index=True)
    producto = db.Column(db.String(160))
    slot_id = db.Column(db.Integer, index=True, default=0)
    monto = db.Column(db.Numeric(10, 2), default=0)
    raw = db.Column(db.Text)  # json crudo
    dispensado = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


# ----------------------------
# Inicialización de DB (idempotente)
# ----------------------------
def ensure_schema():
    """
    Crea tablas y agrega columnas/índices que falten (idempotente, sobre todo útil en Railway+Postgres).
    """
    from sqlalchemy import text

    db.create_all()

    # Si es Postgres, ajusta tipos/índices con SQL idempotente
    try:
        dialect = db.engine.url.get_backend_name()
    except Exception:
        dialect = ""

    if dialect.startswith("postgres"):
        stmts = [
            # producto
            "ALTER TABLE producto ADD COLUMN IF NOT EXISTS slot_id INTEGER NOT NULL DEFAULT 0;",
            "ALTER TABLE producto ADD COLUMN IF NOT EXISTS nombre VARCHAR(120) NOT NULL DEFAULT '';",
            "ALTER TABLE producto ADD COLUMN IF NOT EXISTS precio NUMERIC(10,2) NOT NULL DEFAULT 0;",
            "ALTER TABLE producto ADD COLUMN IF NOT EXISTS cantidad INTEGER NOT NULL DEFAULT 1;",
            "ALTER TABLE producto ADD COLUMN IF NOT EXISTS habilitado BOOLEAN NOT NULL DEFAULT FALSE;",
            "ALTER TABLE producto ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW();",
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname='idx_producto_slot') THEN "
            "CREATE UNIQUE INDEX idx_producto_slot ON producto(slot_id); END IF; END $$;",

            # pago
            "ALTER TABLE pago ADD COLUMN IF NOT EXISTS id_pago VARCHAR(60);",
            "ALTER TABLE pago ADD COLUMN IF NOT EXISTS estado VARCHAR(40);",
            "ALTER TABLE pago ADD COLUMN IF NOT EXISTS producto VARCHAR(160);",
            "ALTER TABLE pago ADD COLUMN IF NOT EXISTS slot_id INTEGER NOT NULL DEFAULT 0;",
            "ALTER TABLE pago ADD COLUMN IF NOT EXISTS monto NUMERIC(10,2) NOT NULL DEFAULT 0;",
            "ALTER TABLE pago ADD COLUMN IF NOT EXISTS raw TEXT;",
            "ALTER TABLE pago ADD COLUMN IF NOT EXISTS dispensado BOOLEAN NOT NULL DEFAULT FALSE;",
            "ALTER TABLE pago ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL DEFAULT NOW();",
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname='idx_pago_id_pago') THEN "
            "CREATE UNIQUE INDEX idx_pago_id_pago ON pago(id_pago); END IF; END $$;",
        ]
        for s in stmts:
            try:
                db.session.execute(text(s))
                db.session.commit()
            except Exception:
                db.session.rollback()


with app.app_context():
    ensure_schema()


# ----------------------------
# Mercado Pago
# ----------------------------
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "").strip()
sdk = MP_SDK(MP_ACCESS_TOKEN) if (MP_SDK and MP_ACCESS_TOKEN) else None

def abs_url(base_env: str | None, fallback_from_request: bool, path: str = "") -> str:
    """
    Devuelve URL absoluta con https.
    - base_env: si viene seteada, se usa esa (sin / final)
    - si fallback_from_request=True, usa request.host_url (con https si corresponde)
    - path: sin slash inicial
    """
    base = (base_env or "").rstrip("/")
    if not base and fallback_from_request:
        # request.host_url ya tiene / al final
        base = (request.host_url or "").rstrip("/")
    return f"{base}/{path.lstrip('/')}"


# ----------------------------
# MQTT (opcional)
# ----------------------------
mqtt_client = None
MQTT_BROKER = os.getenv("MQTT_BROKER", "").strip()
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883") or "1883")
MQTT_USER = os.getenv("MQTT_USER", "").strip()
MQTT_PASS = os.getenv("MQTT_PASS", "").strip()
MQTT_TOPIC = os.getenv("MQTT_TOPIC", "dispen/dispense").strip()  # ej: dispen/dispense

def mqtt_init():
    global mqtt_client
    if not mqtt or not MQTT_BROKER:
        return
    try:
        mqtt_client = mqtt.Client()
        if MQTT_USER or MQTT_PASS:
            mqtt_client.username_pw_set(MQTT_USER, MQTT_PASS)
        mqtt_client.connect(MQTT_BROKER, MQTT_PORT, keepalive=30)
        mqtt_client.loop_start()
        print("[MQTT] conectado a", MQTT_BROKER, MQTT_PORT, "topic:", MQTT_TOPIC, flush=True)
    except Exception as e:
        print("[MQTT] no se pudo conectar:", e, flush=True)
        mqtt_client = None

def mqtt_dispense(slot_id: int):
    if mqtt_client:
        try:
            payload = json.dumps({"event": "dispense", "slot_id": slot_id, "ts": int(datetime.utcnow().timestamp())})
            mqtt_client.publish(MQTT_TOPIC, payload, qos=1, retain=False)
            print("[MQTT] publish", MQTT_TOPIC, payload, flush=True)
        except Exception as e:
            print("[MQTT] publish error:", e, flush=True)

mqtt_init()


# ----------------------------
# Helpers
# ----------------------------
def decimal_to_float(val):
    if isinstance(val, Decimal):
        return float(val)
    return val

def producto_to_dict(p: Producto):
    return {
        "id": p.id,
        "slot_id": p.slot_id,
        "nombre": p.nombre,
        "precio": float(p.precio),
        "cantidad": p.cantidad,
        "habilitado": p.habilitado,
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


# ----------------------------
# Rutas de salud / raíz
# ----------------------------
@app.get("/")
def root():
    return "Backend Dispen-Easy activo 🚀"

@app.get("/healthz")
def healthz():
    return jsonify({"ok": True})


# ----------------------------
# CRUD de productos (mínimo)
# ----------------------------
@app.get("/api/productos")
def get_productos():
    prods = Producto.query.order_by(Producto.slot_id.asc()).all()
    return jsonify([producto_to_dict(p) for p in prods])

@app.post("/api/productos/<int:slot_id>")
def upsert_producto(slot_id: int):
    data = request.get_json(silent=True) or {}
    nombre = (data.get("nombre") or "").strip()
    precio = Decimal(str(data.get("precio", 0) or 0))
    cantidad = int(data.get("cantidad", 1) or 1)
    habilitado = bool(data.get("habilitado", False))

    p = Producto.query.filter_by(slot_id=slot_id).first()
    if not p:
        p = Producto(slot_id=slot_id)
        db.session.add(p)

    p.nombre = nombre
    p.precio = precio
    p.cantidad = cantidad
    p.habilitado = habilitado
    db.session.commit()
    return jsonify({"ok": True, "producto": producto_to_dict(p)})

@app.delete("/api/productos/<int:slot_id>")
def delete_producto(slot_id: int):
    p = Producto.query.filter_by(slot_id=slot_id).first()
    if not p:
        return jsonify({"ok": True})  # idempotente
    db.session.delete(p)
    db.session.commit()
    return jsonify({"ok": True})


# ----------------------------
# Generar preferencia + QR
# ----------------------------
@app.route("/api/generar_qr/<int:slot_id>", methods=["POST", "GET"])
def generar_qr(slot_id: int):
    if sdk is None:
        return jsonify({"error": "MP_ACCESS_TOKEN no configurado o SDK no disponible"}), 500

    p = Producto.query.filter_by(slot_id=slot_id).first()
    if not p or (not p.habilitado) or (not p.nombre) or Decimal(p.precio) <= 0:
        return jsonify({"error": "Producto no válido (habilitado/nombre/precio)"}), 400

    # URLs
    public_url = abs_url(os.getenv("PUBLIC_URL"), fallback_from_request=True)
    noti_url = abs_url(os.getenv("PUBLIC_URL"), fallback_from_request=True, path="webhook")
    back_base = abs_url(os.getenv("FRONTEND_URL"), fallback_from_request=True)

    pref_data = {
        "items": [{
            "title": p.nombre,
            "quantity": 1,
            "unit_price": float(p.precio),
            "currency_id": "ARS",
            "description": p.nombre,
        }],
        "metadata": {
            "producto_id": p.id,
            "slot_id": p.slot_id,
        },
        "external_reference": f"prod:{p.id}",
        "notification_url": noti_url,
        "back_urls": {
            "success": back_base,   # tienen que ser absolutas y https
            "pending": back_base,
            "failure": back_base,
        },
        "auto_return": "approved",
    }

    pref = sdk.preference().create(pref_data)
    status = pref.get("status")
    if status != 201:
        return jsonify({
            "error": "no se pudo crear preferencia",
            "detalle": pref.get("response")
        }), 400

    init_point = pref["response"]["init_point"]

    # Generamos QR local para el init_point
    img = qrcode.make(init_point)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    qr_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
    data_url = f"data:image/png;base64,{qr_b64}"

    return jsonify({
        "ok": True,
        "init_point": init_point,
        "qr": data_url
    })


# ----------------------------
# Webhook Mercado Pago
# ----------------------------
@app.post("/webhook")
def webhook():
    """
    Recibe notificaciones. Si viene un pago, consulta el detalle y guarda en DB.
    """
    try:
        payload = request.get_json(silent=True)
        if not payload:
            # developers MP a veces mandan form-urlencoded
            payload = request.form.to_dict(flat=True) or {}

        print("[webhook] raw:", json.dumps(payload, ensure_ascii=False), flush=True)

        tipo = payload.get("type") or payload.get("topic")
        # Ej: { "type": "payment", "data": {"id": "123"} }
        data = payload.get("data") or {}
        id_pago = str(data.get("id") or payload.get("id") or "")

        if not tipo or not id_pago:
            # Notificación sin info suficiente; igual respondemos 200
            return jsonify({"ok": True})

        estado = None
        monto = Decimal("0")
        descripcion = ""
        slot_id = 0
        raw_detalle = {}

        if sdk and tipo == "payment":
            try:
                det = sdk.payment().get(id_pago)
                raw_detalle = det
                if det.get("status") == 200:
                    body = det.get("response", {}) or {}
                    estado = body.get("status")
                    monto = Decimal(str(body.get("transaction_amount") or 0))
                    descripcion = (body.get("description") or "").strip()
                    meta = body.get("metadata") or {}
                    slot_id = int(meta.get("slot_id") or 0)
            except Exception as e:
                print("[webhook] error consultando pago:", e, flush=True)

        # Guarda en DB (ignora duplicados)
        try:
            pago = Pago(
                id_pago=id_pago,
                estado=estado or (tipo or ""),
                producto=descripcion or "Producto",
                slot_id=slot_id,
                monto=monto,
                raw=json.dumps(raw_detalle or payload, ensure_ascii=False),
                dispensado=False,
            )
            db.session.add(pago)
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            print("[DB] error guardando pago:", e, flush=True)

        # Disparar MQTT si corresponde
        if (estado or "").lower() == "approved" and slot_id:
            mqtt_dispense(slot_id)

        return jsonify({"ok": True})
    except Exception as e:
        print("[webhook] exception:", e, flush=True)
        return jsonify({"ok": True})  # Siempre 200 para que MP no reintente infinito


# ----------------------------
# Debug: listar pagos
# ----------------------------
@app.get("/api/pagos")
def listar_pagos():
    pagos = Pago.query.order_by(Pago.id.desc()).limit(50).all()
    out = []
    for p in pagos:
        out.append({
            "id": p.id,
            "id_pago": p.id_pago,
            "estado": p.estado,
            "producto": p.producto,
            "slot_id": p.slot_id,
            "monto": float(p.monto),
            "dispensado": p.dispensado,
            "created_at": p.created_at.isoformat() if p.created_at else None,
        })
    return jsonify(out)


# ----------------------------
# Main local
# ----------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
