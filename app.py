import os, json
from datetime import datetime
from typing import List, Dict, Any

from flask import Flask, jsonify, request, send_file
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text
from io import BytesIO

# QR
import qrcode

# MQTT (opcional)
try:
    import paho.mqtt.client as mqtt
except Exception:
    mqtt = None

# Mercado Pago SDK
from mercadopago import SDK


# -----------------------------------------------------------------------------
# App & DB
# -----------------------------------------------------------------------------
app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "").replace("postgres://", "postgresql://")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# CORS (permití tu front)
frontend_origin = os.getenv("FRONTEND_ORIGIN", "*")
CORS(app, resources={r"/api/*": {"origins": frontend_origin}}, supports_credentials=True)

db = SQLAlchemy(app)

# Mercado Pago
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN")
sdk = SDK(MP_ACCESS_TOKEN) if MP_ACCESS_TOKEN else None

# MQTT opcional
def _mqtt_client():
    if not mqtt: 
        return None
    broker = os.getenv("MQTT_BROKER")
    if not broker:
        return None
    client = mqtt.Client()
    user = os.getenv("MQTT_USER")
    pwd  = os.getenv("MQTT_PASSWORD")
    if user and pwd:
        client.username_pw_set(user, pwd)
    try:
        client.connect(broker, 1883, 60)
        return client
    except Exception:
        return None


# -----------------------------------------------------------------------------
# Modelos
# -----------------------------------------------------------------------------
class Producto(db.Model):
    __tablename__ = "producto"
    id         = db.Column(db.Integer, primary_key=True)
    slot_id    = db.Column(db.Integer, nullable=False, index=True)    # 1..6
    nombre     = db.Column(db.String(120), nullable=False, default="")
    precio     = db.Column(db.Float, nullable=False, default=0.0)      # ARS
    cantidad   = db.Column(db.Integer, nullable=False, default=1)      # litros (o unidades)
    habilitado = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "slot_id": self.slot_id,
            "nombre": self.nombre,
            "precio": self.precio,
            "cantidad": self.cantidad,
            "habilitado": self.habilitado,
            "created_at": self.created_at.isoformat() + "Z" if self.created_at else None,
        }


class Pago(db.Model):
    __tablename__ = "pago"
    id           = db.Column(db.Integer, primary_key=True)
    id_pago      = db.Column(db.String(64), index=True)   # id payment u order
    estado       = db.Column(db.String(32))               # approved / pending / etc
    producto     = db.Column(db.String(120))
    slot_id      = db.Column(db.Integer)
    monto        = db.Column(db.Float)                    # guardamos entero/float
    raw          = db.Column(db.Text)                     # JSON crudo
    dispensado   = db.Column(db.Boolean, default=False)
    created_at   = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


# -----------------------------------------------------------------------------
# Auto-esquema (idempotente para Railway)
# -----------------------------------------------------------------------------
def ensure_schema():
    """Crea tablas y columnas si faltan (seguro para correr en cada boot)."""
    dialect = db.engine.url.get_backend_name()
    db.create_all()
    if dialect.startswith("postgres"):
        stmts = [
            # columnas producto
            "ALTER TABLE producto ADD COLUMN IF NOT EXISTS slot_id INTEGER NOT NULL DEFAULT 0;",
            "ALTER TABLE producto ADD COLUMN IF NOT EXISTS nombre VARCHAR(120) NOT NULL DEFAULT '';",
            "ALTER TABLE producto ADD COLUMN IF NOT EXISTS precio DOUBLE PRECISION NOT NULL DEFAULT 0;",
            "ALTER TABLE producto ADD COLUMN IF NOT EXISTS cantidad INTEGER NOT NULL DEFAULT 1;",
            "ALTER TABLE producto ADD COLUMN IF NOT EXISTS habilitado BOOLEAN NOT NULL DEFAULT FALSE;",
            "ALTER TABLE producto ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW();",
            # índice único por slot
            """
            DO $$ BEGIN
              IF NOT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname='idx_producto_slot') THEN
                CREATE UNIQUE INDEX idx_producto_slot ON producto(slot_id);
              END IF;
            END $$;
            """,
            # tabla pago columnas
            "ALTER TABLE pago ADD COLUMN IF NOT EXISTS id_pago VARCHAR(64);",
            "ALTER TABLE pago ADD COLUMN IF NOT EXISTS estado VARCHAR(32);",
            "ALTER TABLE pago ADD COLUMN IF NOT EXISTS producto VARCHAR(120);",
            "ALTER TABLE pago ADD COLUMN IF NOT EXISTS slot_id INTEGER;",
            "ALTER TABLE pago ADD COLUMN IF NOT EXISTS monto DOUBLE PRECISION;",
            "ALTER TABLE pago ADD COLUMN IF NOT EXISTS raw TEXT;",
            "ALTER TABLE pago ADD COLUMN IF NOT EXISTS dispensado BOOLEAN DEFAULT FALSE;",
            "ALTER TABLE pago ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW();",
        ]
        with db.engine.begin() as conn:
            for s in stmts:
                conn.execute(text(s))

with app.app_context():
    ensure_schema()


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
SLOTS = [1,2,3,4,5,6]

def fila_vacia(slot_id: int) -> Dict[str, Any]:
    return {
        "id": None, "slot_id": slot_id, "nombre": "", "precio": 0.0,
        "cantidad": 1, "habilitado": False, "created_at": None
    }


# -----------------------------------------------------------------------------
# API Productos (GET/UPSERT/DELETE)
# -----------------------------------------------------------------------------
@app.get("/api/productos")
def get_productos():
    # traigo todos y relleno faltantes con filas vacías
    existentes: List[Producto] = Producto.query.all()
    por_slot = {p.slot_id: p for p in existentes}
    out = []
    for s in SLOTS:
        out.append(por_slot[s].to_dict() if s in por_slot else fila_vacia(s))
    return jsonify(out)

@app.post("/api/productos/<int:slot_id>")
def upsert_producto(slot_id: int):
    data = request.get_json(force=True) or {}
    nombre     = (data.get("nombre") or "").strip()
    precio     = float(data.get("precio") or 0)
    cantidad   = int(data.get("cantidad") or 1)
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
    return jsonify(p.to_dict())

@app.delete("/api/productos/<int:slot_id>")
def delete_producto(slot_id: int):
    p = Producto.query.filter_by(slot_id=slot_id).first()
    if not p:
        return jsonify({"ok": True})
    db.session.delete(p)
    db.session.commit()
    return jsonify({"ok": True})


# -----------------------------------------------------------------------------
# Generar QR (POST desde front y GET directo)
# -----------------------------------------------------------------------------
def _crear_preferencia_para_slot(slot_id: int) -> Dict[str, Any]:
    if not sdk:
        return {"error": "MP_ACCESS_TOKEN no configurado", "status": 500}

    p = Producto.query.filter_by(slot_id=slot_id).first()
    if not p or (not p.habilitado) or (not p.nombre) or (p.precio <= 0):
        return {"error": "Producto inválido/inhabilitado", "status": 400}

    preference_data = {
        "items": [{
            "title": p.nombre,
            "quantity": 1,
            "unit_price": float(p.precio),
            "currency_id": "ARS",
            "description": p.nombre
        }],
        "metadata": {"producto_id": p.id, "slot_id": p.slot_id},
        "external_reference": f"prod:{p.id}",
        "back_urls": {
            "success": "/",
            "pending": "/",
            "failure": "/"
        },
        "auto_return": "approved",
        "notification_url": f"{request.url_root.strip('/')}/webhook"
    }

    pref = sdk.preference().create(preference_data)
    if pref.get("status") != 201:
        detalle = pref.get("response")
        return {"error": "No se pudo crear preferencia", "detalle": detalle, "status": 502}

    return {"ok": True, "pref": pref.get("response"), "slot": p}

def _qr_png_from_url(url: str) -> bytes:
    img = qrcode.make(url)
    bio = BytesIO()
    img.save(bio, format="PNG")
    bio.seek(0)
    return bio.read()

@app.post("/api/generar_qr/<int:slot_id>")
def generar_qr_post(slot_id: int):
    res = _crear_preferencia_para_slot(slot_id)
    if "error" in res:
        return jsonify({"error": res["error"], "detalle": res.get("detalle")}), res.get("status", 400)

    init_point = res["pref"].get("init_point") or res["pref"].get("sandbox_init_point")
    png = _qr_png_from_url(init_point)
    return send_file(BytesIO(png), mimetype="image/png")

@app.get("/api/generar_qr/<int:slot_id>")
def generar_qr_get(slot_id: int):
    # mismo comportamiento pero accesible por GET (útil para abrir en el navegador)
    res = _crear_preferencia_para_slot(slot_id)
    if "error" in res:
        return jsonify({"error": res["error"], "detalle": res.get("detalle")}), res.get("status", 400)

    init_point = res["pref"].get("init_point") or res["pref"].get("sandbox_init_point")
    png = _qr_png_from_url(init_point)
    return send_file(BytesIO(png), mimetype="image/png")


# -----------------------------------------------------------------------------
# Webhook Mercado Pago
# -----------------------------------------------------------------------------
@app.post("/webhook")
def webhook_mp():
    """
    Mercado Pago enviará distintos eventos. 
    Para simplificar: si hay un 'data.id', intentamos consultar el payment,
    guardamos un registro en 'pago' y, si está approved, publicamos por MQTT.
    """
    body = request.get_json(silent=True) or {}
    topic = request.args.get("topic") or body.get("type") or ""
    raw_str = json.dumps(body, ensure_ascii=False)

    # Valores por defecto
    id_pago = str(body.get("data", {}).get("id") or body.get("resource") or "unknown")
    estado = "pendiente"
    monto = None
    prod_nombre = None
    slot_id = None

    try:
        if sdk and id_pago.isdigit() and (topic in ("payment", "merchant_order", "payment.updated", "payment.created")):
            # Obtener info del pago
            py = sdk.payment().get(id_pago)
            info = py.get("response", {}) if py else {}
            estado = (info.get("status") or estado)
            monto = info.get("transaction_amount")

            md = info.get("metadata") or {}
            slot_id = md.get("slot_id")
            # el nombre del item puede venir en additional_info o en items
            items = (info.get("additional_info", {}) or {}).get("items") or []
            if items and items[0].get("title"):
                prod_nombre = items[0]["title"]

            # fallback por metadata
            if not prod_nombre:
                prod = Producto.query.filter_by(slot_id=slot_id).first()
                prod_nombre = prod.nombre if prod else None

    except Exception as e:
        # solo log, seguimos guardando raw
        print("[webhook] error:", e, flush=True)

    # Guardar registro
    try:
        reg = Pago(
            id_pago=str(id_pago),
            estado=str(estado),
            producto=prod_nombre,
            slot_id=slot_id if isinstance(slot_id, int) else None,
            monto=float(monto) if monto is not None else None,
            raw=raw_str,
            dispensado=False,
        )
        db.session.add(reg)
        db.session.commit()
    except Exception as e:
        print("[DB] error guardando pago:", e, flush=True)

    # MQTT si approved
    if str(estado).lower() == "approved":
        client = _mqtt_client()
        if client:
            try:
                # Publicamos el slot aprobado (si no tenemos, publicamos 0)
                client.publish("dispen/approved", json.dumps({
                    "slot_id": slot_id or 0,
                    "id_pago": id_pago
                }), qos=1)
                client.disconnect()
            except Exception as e:
                print("[MQTT] error publish:", e, flush=True)

    return jsonify({"ok": True})


# -----------------------------------------------------------------------------
# Salud
# -----------------------------------------------------------------------------
@app.get("/")
def root():
    return jsonify({"mensaje": "API de Dispen-Easy funcionando"})


# -----------------------------------------------------------------------------
# Run local
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
