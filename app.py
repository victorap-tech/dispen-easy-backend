# app.py
import os
import json
from datetime import datetime

from flask import Flask, jsonify, request
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text

# ---- Opcional MQTT ----
try:
    import paho.mqtt.client as mqtt
except Exception:
    mqtt = None

# ---- Mercado Pago SDK ----
import mercadopago


# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
app = Flask(__name__)
CORS(app)

# DB URL (Railway expone DATABASE_URL)
db_url = os.getenv("SQLALCHEMY_DATABASE_URI") or os.getenv("DATABASE_URL")
if not db_url:
    raise RuntimeError("Falta DATABASE_URL / SQLALCHEMY_DATABASE_URI")

# Acomodar el prefijo para SQLAlchemy si viene de Heroku u otros
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

# MP
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "")
MP_PUBLIC_KEY   = os.getenv("MP_PUBLIC_KEY", "")
sdk = mercadopago.SDK(MP_ACCESS_TOKEN) if MP_ACCESS_TOKEN else None

# Base URL (para back_urls y webhook)
BACKEND_BASE_URL = os.getenv("BACKEND_BASE_URL")  # ej: https://web-production-xxxx.up.railway.app


# -----------------------------------------------------------------------------
# Modelos
# -----------------------------------------------------------------------------
class Producto(db.Model):
    __tablename__ = "producto"
    id         = db.Column(db.Integer, primary_key=True)
    slot_id    = db.Column(db.Integer, nullable=False, default=0, index=True)
    nombre     = db.Column(db.String(120), nullable=False, default="")
    precio     = db.Column(db.Float, nullable=False, default=0.0)
    cantidad   = db.Column(db.Integer, nullable=False, default=1)  # litros
    habilitado = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=True)

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
    id        = db.Column(db.Integer, primary_key=True)
    id_pago   = db.Column(db.String(64), index=True)      # payment_id de MP o merchant_order id
    estado    = db.Column(db.String(32), default="pendiente")
    producto  = db.Column(db.String(120), default="")
    slot_id   = db.Column(db.Integer, nullable=True)
    monto     = db.Column(db.Float, nullable=True)        # en ARS
    raw       = db.Column(db.Text, nullable=True)
    dispensado = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def fila_vacia(slot_id: int) -> dict:
    return {
        "id": None, "slot_id": slot_id, "nombre": "",
        "precio": 0.0, "cantidad": 1, "habilitado": False
    }


def base_url():
    # Preferí la var de entorno para no depender de Host header
    if BACKEND_BASE_URL:
        return BACKEND_BASE_URL.rstrip("/")
    # fallback: request.url_root (con trailing slash)
    return request.url_root.rstrip("/")


def mqtt_publish(topic: str, payload: str):
    broker = os.getenv("MQTT_BROKER")
    if not broker or mqtt is None:
        return
    try:
        client = mqtt.Client()
        client.connect(broker, int(os.getenv("MQTT_PORT", "1883")))
        client.publish(topic, payload, qos=1)
        client.disconnect()
    except Exception as e:
        print("[MQTT] error publicando:", e, flush=True)


# -----------------------------------------------------------------------------
# Asegurar esquema (idempotente para Railway)
# -----------------------------------------------------------------------------
def ensure_schema():
    db.create_all()  # crea tablas si no existen

    try:
        engine = db.engine
        if engine.url.get_backend_name().startswith("postgres"):
            stmts = [
                # producto
                "ALTER TABLE producto ADD COLUMN IF NOT EXISTS slot_id INTEGER NOT NULL DEFAULT 0;",
                "ALTER TABLE producto ADD COLUMN IF NOT EXISTS nombre VARCHAR(120) NOT NULL DEFAULT '';",
                "ALTER TABLE producto ADD COLUMN IF NOT EXISTS precio DOUBLE PRECISION NOT NULL DEFAULT 0;",
                "ALTER TABLE producto ADD COLUMN IF NOT EXISTS cantidad INTEGER NOT NULL DEFAULT 1;",
                "ALTER TABLE producto ADD COLUMN IF NOT EXISTS habilitado BOOLEAN NOT NULL DEFAULT FALSE;",
                "ALTER TABLE producto ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW();",
                "ALTER TABLE producto ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITHOUT TIME ZONE;",
                # índice único por slot (opcional, si querés solo 1 fila por slot)
                """
                DO $$
                BEGIN
                  IF NOT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = 'idx_producto_slot') THEN
                    CREATE UNIQUE INDEX idx_producto_slot ON producto(slot_id);
                  END IF;
                END $$;
                """,

                # pago
                "ALTER TABLE pago ADD COLUMN IF NOT EXISTS id_pago VARCHAR(64);",
                "ALTER TABLE pago ADD COLUMN IF NOT EXISTS estado VARCHAR(32) DEFAULT 'pendiente';",
                "ALTER TABLE pago ADD COLUMN IF NOT EXISTS producto VARCHAR(120) DEFAULT '';",
                "ALTER TABLE pago ADD COLUMN IF NOT EXISTS slot_id INTEGER;",
                "ALTER TABLE pago ADD COLUMN IF NOT EXISTS monto DOUBLE PRECISION;",
                "ALTER TABLE pago ADD COLUMN IF NOT EXISTS raw TEXT;",
                "ALTER TABLE pago ADD COLUMN IF NOT EXISTS dispensado BOOLEAN DEFAULT FALSE;",
                "ALTER TABLE pago ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW();",
            ]
            with engine.begin() as conn:
                for s in stmts:
                    conn.execute(text(s))
            print("[DB] esquema OK", flush=True)
    except Exception as e:
        print("[DB] ensure_schema error:", e, flush=True)


with app.app_context():
    ensure_schema()


# -----------------------------------------------------------------------------
# Rutas
# -----------------------------------------------------------------------------
@app.get("/")
def raiz():
    return jsonify({"mensaje": "API de Dispen-Easy funcionando"})


@app.get("/api/productos")
def get_productos():
    # Siempre devolver 6 filas (slots 1..6). Si falta alguna, completar vacía.
    existentes = {p.slot_id: p for p in Producto.query.order_by(Produto.slot_id).all()} if False else {p.slot_id: p for p in Producto.query.all()}
    out = []
    for slot in range(1, 7):
        if slot in existentes:
            out.append(existentes[slot].to_dict())
        else:
            out.append(fila_vacia(slot))
    return jsonify(out)


@app.post("/api/productos/<int:slot_id>")
def upsert_producto(slot_id: int):
    data = request.get_json(force=True) or {}
    nombre = (data.get("nombre") or "").strip()
    precio = float(data.get("precio") or 0)
    cantidad = int(data.get("cantidad") or 1)
    habilitado = bool(data.get("habilitado"))

    p = Producto.query.filter_by(slot_id=slot_id).first()
    if p is None:
        p = Producto(slot_id=slot_id)
        db.session.add(p)

    p.nombre = nombre
    p.precio = precio
    p.cantidad = cantidad
    p.habilitado = habilitado
    p.updated_at = datetime.utcnow()
    db.session.commit()

    return jsonify({"ok": True, "producto": p.to_dict()}), 200


@app.delete("/api/productos/<int:slot_id>")
def delete_producto(slot_id: int):
    p = Producto.query.filter_by(slot_id=slot_id).first()
    if not p:
        return jsonify({"ok": True})  # idempotente
    db.session.delete(p)
    db.session.commit()
    return jsonify({"ok": True})


@app.route("/api/generar_qr/<int:slot_id>", methods=["POST", "GET"])
def generar_qr(slot_id: int):
    if sdk is None:
        return jsonify({"error": "MP_ACCESS_TOKEN no configurado"}), 500
    
    p = Producto.query.filter_by(slot_id=slot_id).first()
    if not p or not p.habilitado or not p.nombre or p.precio <= 0:
        return jsonify({"error": "Producto no válido"}), 400

    # --- tu lógica MercadoPago actual ---

    pref_data = {
        "items": [{
            "title": p.nombre,
            "quantity": 1,
            "unit_price": float(p.precio),
            "currency_id": "ARS",
            "description": p.nombre
        }],
        "metadata": {
            "producto_id": p.id,
            "slot_id": p.slot_id
        },
        "external_reference": f"prod:{p.id}",
        "notification_url": f"{base_url()}/webhook",
        "back_urls": {
            "success": f"{base_url()}/",
            "pending": f"{base_url()}/",
            "failure": f"{base_url()}/",
        },
        "auto_return": "approved"
    }

    try:
        resp = sdk.preference().create(pref_data)
        if resp.get("status") != 201:
            return jsonify({"error": "No se pudo crear preferencia", "detalle": resp}), 500

        init_point = resp["response"].get("init_point") or resp["response"].get("sandbox_init_point")
        # Guardar el pago en estado pendiente (id ficticio hasta que llegue el webhook)
        pg = Pago(
            id_pago="PENDIENTE",
            estado="pendiente",
            producto=p.nombre,
            slot_id=p.slot_id,
            monto=p.precio,
            raw=json.dumps(resp.get("response", {}))
        )
        db.session.add(pg)
        db.session.commit()

        return jsonify({
            "ok": True,
            "init_point": init_point,
            "public_key": MP_PUBLIC_KEY,  # por si usás el Brick
        })
    except Exception as e:
        print("[MP] error pref:", e, flush=True)
        return jsonify({"error": "Error creando preferencia"}), 500


@app.post("/webhook")
def webhook():
    """
    Maneja notificaciones de MP:
    - topic 'payment' con 'data.id' => GET /v1/payments/{id}
    - topic 'merchant_order' => GET merchant_orders/{id}
    """
    try:
        raw = request.get_json(force=True) or {}
    except Exception:
        raw = {}

    print("[webhook] raw:", raw, flush=True)

    topic = raw.get("type") or raw.get("topic")
    payment_id = None

    if topic == "payment":
        # MP v1
        payment_id = str((raw.get("data") or {}).get("id") or "").strip()
    elif topic == "merchant_order":
        # Podríamos buscar los payments dentro de la order
        merchant_order_id = str(raw.get("data", {}).get("id") or "")
        # para simplificar, guardamos la MO como fila y salimos OK
        pg = Pago(id_pago=f"MO:{merchant_order_id}", estado="pendiente", raw=json.dumps(raw))
        db.session.add(pg)
        db.session.commit()
        return jsonify({"status": "ok"}), 200

    # Si vino un payment_id, consultamos detalles
    if payment_id and sdk:
        try:
            r = sdk.payment().get(payment_id)
            st = r.get("status")
            body = r.get("response", {})
            status_detail = body.get("status")
            description = ""
            monto = None
            slot_id = None

            # metadata que cargamos en la preferencia
            md = body.get("metadata") or {}
            slot_id = md.get("slot_id")
            description = body.get("description") or ""

            try:
                monto = float(body.get("transaction_amount") or 0)
            except Exception:
                monto = None

            # upsert pago
            pg = Pago.query.filter_by(id_pago=payment_id).first()
            if not pg:
                pg = Pago(id_pago=payment_id)
                db.session.add(pg)

            pg.estado = status_detail or st or "desconocido"
            pg.producto = description or pg.producto
            if slot_id:
                pg.slot_id = int(slot_id)
            if monto is not None:
                pg.monto = monto
            pg.raw = json.dumps(body)
            db.session.commit()

            # Si aprobado → mandar MQTT
            if (status_detail or "").lower() == "approved" and pg.slot_id:
                topic_out = os.getenv("MQTT_TOPIC", "dispen/abrir")
                mqtt_publish(topic_out, str(pg.slot_id))

            return jsonify({"status": "ok"}), 200
        except Exception as e:
            print("[webhook] Error consultando payment:", e, flush=True)
            return jsonify({"status": "error consultando payment"}), 200

    # Si no sabemos qué es, igual devolvemos 200
    return jsonify({"status": "ok"}), 200


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
