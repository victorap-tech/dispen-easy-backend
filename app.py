# ============================================================
# Dispen-Easy backend: Flask + SQLAlchemy + MercadoPago + MQTT
# - /                → "Dispen-Easy backend activo"
# - /api/productos   → CRUD simple
# - /api/generar_qr  → crea preferencia MP y devuelve QR (link embebido)
# - /webhook         → recibe notificación, verifica pago y publica MQTT
# - /admin/migrate   → agrega slot_id/created_at/updated_at (idempotente)
# - /admin/dbinfo    → inspección de columnas
# ============================================================

import os
import io
import base64
from datetime import datetime

from flask import Flask, jsonify, request, Blueprint
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, text
from sqlalchemy.sql import func

# ---- Opcionales instalados vía requirements.txt ----
import qrcode
import mercadopago
import paho.mqtt.client as mqtt

# ---------------------------
# Flask & DB
# ---------------------------
app = Flask(__name__)
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///dispen_easy.db")
app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

CORS(app, resources={r"/api/*": {"origins": "*"}})

# ---------------------------
# Modelo
# ---------------------------
class Producto(db.Model):
    __tablename__ = "producto"
    id         = db.Column(db.Integer, primary_key=True)
    nombre     = db.Column(db.String(120), nullable=False)
    precio     = db.Column(db.Float, nullable=False)            # ARS
    cantidad   = db.Column(db.Integer, nullable=False, default=0)  # Litros (o unidades)
    slot_id    = db.Column(db.Integer, nullable=False, default=1)  # Salida 1..6
    created_at = db.Column(db.DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), onupdate=func.now())

    def to_dict(self):
        return {
            "id": self.id,
            "nombre": self.nombre,
            "precio": self.precio,
            "cantidad": self.cantidad,
            "slot_id": self.slot_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

with app.app_context():
    db.create_all()

# ---------------------------
# Estado
# ---------------------------
@app.get("/")
def root():
    return "Dispen-Easy backend activo"

@app.get("/health")
def health():
    return {"ok": True, "time": datetime.utcnow().isoformat()}

# ---------------------------
# CRUD Productos
# ---------------------------
@app.get("/api/productos")
def listar_productos():
    slot_id = request.args.get("slot_id", type=int)
    q = Producto.query
    if slot_id:
        q = q.filter_by(slot_id=slot_id)
    prods = q.order_by(Producto.id.asc()).all()
    return jsonify([p.to_dict() for p in prods])

@app.post("/api/productos")
def crear_producto():
    try:
        data = request.get_json(force=True) or {}
        p = Producto(
            nombre=(data.get("nombre") or "").strip(),
            precio=float(data.get("precio")),
            cantidad=int(data.get("cantidad")),
            slot_id=int(data.get("slot_id")),
        )
        db.session.add(p)
        db.session.commit()
        return jsonify(p.to_dict()), 201
    except Exception as e:
        db.session.rollback()
        return jsonify({"detail": str(e)}), 400

@app.delete("/api/productos/<int:pid>")
def eliminar_producto(pid):
    try:
        p = Producto.query.get(pid)
        if not p:
            return jsonify({"detail": "producto no encontrado"}), 404
        db.session.delete(p)
        db.session.commit()
        return jsonify({"ok": True})
    except Exception as e:
        db.session.rollback()
        return jsonify({"detail": str(e)}), 500

# ---------------------------
# Mercado Pago + QR
# ---------------------------
MP_TOKEN = os.getenv("MP_ACCESS_TOKEN")
mp_sdk = mercadopago.SDK(MP_TOKEN) if MP_TOKEN else None

@app.get("/api/generar_qr/<int:pid>")
def generar_qr(pid: int):
    """
    Crea preferencia MP y devuelve:
      - mp_url: link de pago
      - qr_base64: PNG del QR con ese link (al escanear abre MP directo)
    """
    try:
        if not mp_sdk:
            return jsonify({"detail": "Configura MP_ACCESS_TOKEN en Railway"}), 500

        p = Producto.query.get(pid)
        if not p:
            return jsonify({"detail": "Producto no encontrado"}), 404

        slot_id = request.args.get("slot_id", type=int) or p.slot_id

        notification_url = request.url_root.rstrip("/") + "/webhook"

        pref_data = {
            "items": [{
                "title": p.nombre,
                "quantity": 1,
                "unit_price": float(p.precio),
                "currency_id": "ARS",
            }],
            "external_reference": f"prod-{p.id}-slot-{slot_id}",
            "auto_return": "approved",
            "notification_url": notification_url,
        }

        pref_res = mp_sdk.preference().create(pref_data)
        resp = pref_res.get("response", {}) if isinstance(pref_res, dict) else {}
        mp_url = resp.get("init_point") or resp.get("sandbox_init_point")
        if not mp_url:
            return jsonify({"detail": "Mercado Pago no devolvió init_point", "mp_response": resp}), 400

        img = qrcode.make(mp_url)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        qr_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

        return jsonify({"mp_url": mp_url, "qr_base64": qr_b64}), 200

    except Exception as e:
        app.logger.exception(e)
        return jsonify({"detail": str(e)}), 500

# ---------------------------
# MQTT (publicación al aprobar pago)
# ---------------------------
MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USERNAME = os.getenv("MQTT_USERNAME")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD")
MQTT_TOPIC_PREFIX = os.getenv("MQTT_TOPIC_PREFIX", "dispen")

def publish_mqtt(payload: dict):
    try:
        client = mqtt.Client()
        if MQTT_USERNAME:
            client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD or "")
        client.connect(MQTT_HOST, MQTT_PORT, keepalive=10)
        topic = f"{MQTT_TOPIC_PREFIX}/dispense"
        client.publish(topic, payload=str(payload), qos=1, retain=False)
        client.disconnect()
        app.logger.info(f"[MQTT] {topic} {payload}")
    except Exception as e:
        app.logger.exception(f"[MQTT] error: {e}")

# ---------------------------
# Webhook MP
# ---------------------------
@app.post("/webhook")
def webhook():
    """
    Recibe notificaciones de MP.
    Si es de tipo 'payment', consulta el pago y si está 'approved'
    usa external_reference para saber qué slot/producto accionar y publica MQTT.
    """
    try:
        info = request.get_json(silent=True) or {}
        _type = (info.get("type") or info.get("topic") or "").lower()
        data_id = info.get("data", {}).get("id") or info.get("id")

        app.logger.info(f"[WEBHOOK] type={_type} id={data_id} body={info}")

        if not mp_sdk:
            return jsonify({"ok": False, "detail": "MP SDK no init"}), 200

        if _type == "payment" and data_id:
            pay = mp_sdk.payment().get(data_id)
            presp = pay.get("response", {}) if isinstance(pay, dict) else {}
            status = (presp.get("status") or "").lower()
            extref = presp.get("external_reference") or ""
            app.logger.info(f"[WEBHOOK] status={status} external_reference={extref}")

            if status == "approved" and extref.startswith("prod-"):
                # external_reference: "prod-<id>-slot-<slot>"
                try:
                    parts = extref.split("-")
                    prod_id = int(parts[1])
                    slot_id = int(parts[3]) if len(parts) >= 4 else 1
                except Exception:
                    prod_id, slot_id = None, 1

                # Podés buscar el producto para obtener cantidad, etc.
                p = Producto.query.get(prod_id) if prod_id else None
                cantidad_litros = p.cantidad if p else 1

                # Publicar a MQTT -> tu firmware escucha y dispensa
                publish_mqtt({"slot": slot_id, "litros": cantidad_litros, "product_id": prod_id, "ts": datetime.utcnow().isoformat()})
                return jsonify({"ok": True})

        # Otros tipos (merchant_order, etc.) los ignoramos por simplicidad
        return jsonify({"ok": True})
    except Exception as e:
        app.logger.exception(e)
        return jsonify({"ok": False, "detail": str(e)}), 200

# ---------------------------
# Admin: migración + dbinfo
# ---------------------------
migrate_bp = Blueprint("migrate_bp", __name__)

def _exec(engine, sql):
    with engine.begin() as conn:
        conn.execute(text(sql))

@migrate_bp.get("/migrate")
def admin_migrate():
    token = request.args.get("token")
    expected = os.getenv("MIGRATION_TOKEN")
    if not expected or token != expected:
        return jsonify({"ok": False, "detail": "forbidden"}), 403

    engine = db.engine
    insp = inspect(engine)
    actions = []

    try:
        if "producto" not in insp.get_table_names():
            return jsonify({"ok": False, "detail": "tabla 'producto' no existe"}), 400

        cols = {c["name"] for c in insp.get_columns("producto")}
        dialect = engine.url.get_dialect().name

        if "slot_id" not in cols:
            _exec(engine, "ALTER TABLE producto ADD COLUMN slot_id INTEGER NOT NULL DEFAULT 1;")
            actions.append("ADD slot_id")
        else:
            actions.append("slot_id ya existe")

        if "created_at" not in cols:
            if "postgres" in dialect:
                _exec(engine, "ALTER TABLE producto ADD COLUMN created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();")
            else:
                _exec(engine, "ALTER TABLE producto ADD COLUMN created_at DATETIME NOT NULL DEFAULT (CURRENT_TIMESTAMP);")
            actions.append("ADD created_at")
        else:
            actions.append("created_at ya existe")

        if "updated_at" not in cols:
            if "postgres" in dialect:
                _exec(engine, "ALTER TABLE producto ADD COLUMN updated_at TIMESTAMPTZ;")
            else:
                _exec(engine, "ALTER TABLE producto ADD COLUMN updated_at DATETIME;")
            actions.append("ADD updated_at")
        else:
            actions.append("updated_at ya existe")

        try:
            _exec(engine, "CREATE INDEX IF NOT EXISTS idx_producto_slot_id ON producto (slot_id);")
            actions.append("INDEX slot_id OK")
        except Exception as ie:
            actions.append(f"INDEX SKIP/ERR: {ie}")

        return jsonify({"ok": True, "dialect": dialect, "actions": actions})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "actions": actions}), 500

@migrate_bp.get("/dbinfo")
def admin_dbinfo():
    token = request.args.get("token")
    expected = os.getenv("MIGRATION_TOKEN")
    if not expected or token != expected:
        return jsonify({"ok": False, "detail": "forbidden"}), 403

    engine = db.engine
    insp = inspect(engine)
    data = {"ok": True, "dialect": engine.url.get_dialect().name, "tables": insp.get_table_names(), "columns": {}}
    for t in data["tables"]:
        safe_cols = []
        for c in insp.get_columns(t):
            safe_cols.append({
                "name": c.get("name"),
                "type": str(c.get("type")),
                "nullable": bool(c.get("nullable")),
                "default": str(c.get("default")),
            })
        data["columns"][t] = safe_cols
    return jsonify(data)

app.register_blueprint(migrate_bp, url_prefix="/admin")

# ---------------------------
# Main (local)
# ---------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
