# app.py
import os
import ssl
import json
import base64
import io
import traceback

from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from sqlalchemy import text

import requests
import qrcode

# ======================= App & CORS =======================
app = Flask(__name__)

CORS(
    app,
    resources={r"/api/*": {"origins": [
        # agregá tu dominio de front si cambia
        "https://dispen-easy-web-production.up.railway.app",
        "http://localhost:3000",
    ]}},
    methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)

# ======================= DB =======================
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL")
db = SQLAlchemy(app)

# ======================= MQTT =======================
import paho.mqtt.client as mqtt

def _mqtt_client():
    broker   = os.getenv("MQTT_BROKER")
    port     = int(os.getenv("MQTT_PORT", "8883"))
    user     = os.getenv("MQTT_USER")
    pwd      = os.getenv("MQTT_PASS")
    client_id = os.getenv("MQTT_CLIENT_ID", "dispen-easy-backend")

    if not all([broker, user, pwd]):
        raise RuntimeError("Faltan variables MQTT_BROKER / MQTT_USER / MQTT_PASS")

    c = mqtt.Client(client_id=client_id, clean_session=True)
    c.username_pw_set(user, pwd)
    # TLS seguro
    c.tls_set(tls_version=ssl.PROTOCOL_TLS, cert_reqs=ssl.CERT_REQUIRED)
    c.tls_insecure_set(False)
    return c, broker, port

def mqtt_publish(payload: dict, topic: str = None, qos: int = 1, retain: bool = False):
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

# ======================= Mercado Pago helpers =======================
def mp_get_payment(payment_id: str, token: str):
    url = f"https://api.mercadopago.com/v1/payments/{payment_id}"
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.get(url, headers=headers, timeout=12)
    raw = r.text
    if r.status_code != 200:
        return None, r.status_code, raw
    return r.json(), r.status_code, raw

def mp_get_merchant_order(mo_id: str, token: str):
    url = f"https://api.mercadopago.com/merchant_orders/{mo_id}"
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.get(url, headers=headers, timeout=12)
    raw = r.text
    if r.status_code != 200:
        return None, r.status_code, raw
    return r.json(), r.status_code, raw

# ======================= Modelos =======================
class Pago(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    id_pago = db.Column(db.String(120), unique=True, nullable=False)
    estado = db.Column(db.String(80), nullable=True)
    producto = db.Column(db.String(120), nullable=True)
    dispensado = db.Column(db.Boolean, default=False)

class Producto(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(120), nullable=False)
    precio = db.Column(db.Float, nullable=False)
    cantidad = db.Column(db.Integer, nullable=False, default=1)
    slot_id = db.Column(db.Integer, nullable=False, default=0)  # pin GPIO

    def to_dict(self):
        return {
            "id": self.id,
            "nombre": self.nombre,
            "precio": self.precio,
            "cantidad": self.cantidad,
            "slot_id": self.slot_id,
        }

# ======================= Migración slot_id =======================
def ensure_slot_id_column():
    try:
        res = db.session.execute(text("PRAGMA table_info(producto)"))  # SQLite fallback
        cols = [row[1] for row in res]
    except Exception:
        # Postgres
        res = db.session.execute(
            text("SELECT column_name FROM information_schema.columns "
                 "WHERE table_name='producto'")
        )
        cols = [row[0] for row in res]
    if "slot_id" not in cols:
        try:
            db.session.execute(text("ALTER TABLE producto ADD COLUMN slot_id INTEGER NOT NULL DEFAULT 0"))
            db.session.commit()
            print("[DB] Columna slot_id agregada")
        except Exception as e:
            db.session.rollback()
            print("[DB] Error creando columna slot_id:", e)

# (opcional pero recomendado) Único por slot
def ensure_slot_unique():
    try:
        db.session.execute(text(
            "ALTER TABLE producto ADD CONSTRAINT producto_slot_unique UNIQUE (slot_id)"
        ))
        db.session.commit()
    except Exception:
        db.session.rollback()  # ya existirá

# ======================= Boot DB =======================
with app.app_context():
    db.create_all()
    ensure_slot_id_column()
    ensure_slot_unique()
    print("Tablas listas")

# ======================= Config 6 slots fijos =======================
PIN_MAP = [5, 18, 19, 21, 22, 23]  # slot 1..6

# ======================= Rutas básicas =======================
@app.route("/")
def health():
    return jsonify({"mensaje": "API de Dispen-Easy funcionando"}), 200

# ======================= CRUD Productos (6 fijos) =======================
@app.route("/api/productos", methods=["GET"])
def listar_productos():
    """
    Devuelve siempre 6 filas (una por slot). Si no hay en DB, devuelve fila vacía.
    """
    try:
        rows = db.session.execute(
            text("SELECT id, nombre, precio, cantidad, slot_id FROM producto")
        ).mappings().all()
        by_slot = {r["slot_id"]: dict(r) for r in rows}

        data = []
        for idx, pin in enumerate(PIN_MAP, start=1):
            row = by_slot.get(pin)
            if row:
                row["slot"] = idx
                data.append(row)
            else:
                data.append({
                    "id": None, "nombre": "", "precio": 0, "cantidad": 0,
                    "slot_id": pin, "slot": idx
                })
        return jsonify(data), 200
    except Exception as e:
        print("[GET /api/productos] ERROR:", e, flush=True)
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route("/api/productos", methods=["POST"])
def upsert_producto():
    """
    Upsert por slot_id. Body: { slot_id, nombre, precio, cantidad }
    """
    data = request.get_json(force=True) or {}
    try:
        slot_id  = int(data.get("slot_id"))
        if slot_id not in PIN_MAP:
            return jsonify({"error": "slot_id inválido"}), 400

        nombre   = (data.get("nombre") or "").strip()
        precio   = float(data.get("precio") or 0)
        cantidad = int(data.get("cantidad") or 0)

        # ¿existe fila de ese slot?
        row = db.session.execute(
            text("SELECT id FROM producto WHERE slot_id=:s"),
            {"s": slot_id}
        ).mappings().first()

        if row:
            db.session.execute(
                text("""UPDATE producto
                        SET nombre=:n, precio=:p, cantidad=:c
                        WHERE slot_id=:s"""),
                {"n": nombre, "p": precio, "c": cantidad, "s": slot_id}
            )
        else:
            db.session.execute(
                text("""INSERT INTO producto (nombre, precio, cantidad, slot_id)
                        VALUES (:n, :p, :c, :s)"""),
                {"n": nombre, "p": precio, "c": cantidad, "s": slot_id}
            )
        db.session.commit()
        return jsonify({"ok": True}), 201
    except Exception as e:
        db.session.rollback()
        print("[POST /api/productos] ERROR:", e, flush=True)
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route("/api/productos/<int:slot_id>", methods=["DELETE"])
def vaciar_slot(slot_id: int):
    """
    “Eliminar” un slot: borra la fila de DB. GET volverá a mostrarlo vacío.
    """
    if slot_id not in PIN_MAP:
        return jsonify({"error": "slot_id inválido"}), 400
    try:
        db.session.execute(text("DELETE FROM producto WHERE slot_id=:s"), {"s": slot_id})
        db.session.commit()
        return jsonify({"ok": True}), 200
    except Exception as e:
        db.session.rollback()
        print("[DELETE /api/productos] ERROR:", e, flush=True)
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

# ======================= Generar QR / preferencia (MP) =======================
@app.route("/api/generar_qr/<int:slot>", methods=["GET"])
def generar_qr(slot: int):
    """
    Crea preferencia para el producto del slot y devuelve {qr_base64, link}
    """
    try:
        if slot < 1 or slot > 6:
            return jsonify({"error": "slot inválido (1..6)"}), 400
        slot_id = PIN_MAP[slot-1]

        prod = db.session.execute(
            text("SELECT id, nombre, precio, slot_id FROM producto WHERE slot_id=:s"),
            {"s": slot_id}
        ).mappings().first()
        if not prod:
            return jsonify({"error": "No hay producto configurado en ese slot"}), 404

        token = os.getenv("MP_ACCESS_TOKEN")
        if not token:
            return jsonify({"error": "Falta MP_ACCESS_TOKEN"}), 500

        titulo_visible = prod["nombre"] or f"Producto {slot}"
        payload = {
            "items": [{
                "title": titulo_visible,
                "quantity": 1,
                "unit_price": float(prod["precio"]),
            }],
            "description": prod["nombre"],
            "additional_info": {"items": [{"title": prod["nombre"]}]},
            "metadata": {
                "product_id": prod["id"],
                "slot_id": prod["slot_id"],  # <— clave para el ESP
            },
            "external_reference": f"prod:{prod['id']}",
            "notification_url": "https://web-production-e7d2.up.railway.app/webhook",
            "back_urls": {
                "success": "https://dispen-easy-web-production.up.railway.app/",
                "pending": "https://dispen-easy-web-production.up.railway.app/",
                "failure": "https://dispen-easy-web-production.up.railway.app/",
            },
            "auto_return": "approved",
        }

        url = "https://api.mercadopago.com/checkout/preferences"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        resp = requests.post(url, headers=headers, json=payload, timeout=12)
        if resp.status_code != 201:
            print("MP error:", resp.status_code, resp.text, flush=True)
            return jsonify({"error": "No se pudo generar link de pago"}), 502

        link = resp.json().get("init_point")

        # QR base64
        buf = io.BytesIO()
        qrcode.make(link).save(buf, format="PNG")
        qr_base64 = base64.b64encode(buf.getvalue()).decode("utf-8")

        return jsonify({"qr_base64": qr_base64, "link": link}), 200
    except Exception as e:
        print("[/api/generar_qr] ERROR:", e, flush=True)
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

# ======================= Webhook (MP -> MQTT) =======================
@app.route("/webhook", methods=["POST"])
def webhook():
    raw = request.get_json(silent=True) or {}
    print("[webhook] raw:", raw, flush=True)

    token = os.getenv("MP_ACCESS_TOKEN")
    if not token:
        print("[webhook] Falta MP_ACCESS_TOKEN", flush=True)
        return jsonify({"error": "missing token"}), 500

    # Resolver payment_id
    payment_id = None
    # formato nuevo {"data":{"id":...}}
    if isinstance(raw.get("data"), dict) and raw["data"].get("id"):
        payment_id = str(raw["data"]["id"])
    # formato clásico (merchant_order)
    if not payment_id and raw.get("resource") and raw.get("topic"):
        topic = raw["topic"]
        resource = str(raw["resource"]).rstrip("/")
        if topic == "payment" and "/payments/" in resource:
            payment_id = resource.split("/")[-1]
        elif topic == "merchant_order" and "/merchant_orders/" in resource:
            mo_id = resource.split("/")[-1]
            mo, st, _ = mp_get_merchant_order(mo_id, token)
            if mo:
                payments = mo.get("payments") or []
                if payments:
                    payment_id = str(payments[-1].get("id"))

    if not payment_id:
        print("[webhook] sin payment_id -> ignored", flush=True)
        return jsonify({"status": "ignored"}), 200

    # Traer detalle del pago
    data, st, raw_json = mp_get_payment(payment_id, token)
    print("[webhook] payment:", st, "; id:", data.get("id") if data else None,
          "; status:", data.get("status") if data else None, flush=True)

    if not data:
        return jsonify({"status": "ok"}), 200

    estado = (data.get("status") or "").lower()

    # nombre/slot desde metadata o descripción
    slot_id = (data.get("metadata") or {}).get("slot_id") \
           or (data.get("additional_info") or {}).get("items", [{}])[0].get("title") \
           or None
    try:
        slot_id = int(slot_id)
    except Exception:
        slot_id = None

    # Guardar / actualizar pago
    try:
        reg = Pago.query.filter_by(id_pago=str(payment_id)).first()
        producto_nom = (
            data.get("description")
            or (data.get("additional_info") or {}).get("items", [{}])[0].get("title")
            or (data.get("metadata") or {}).get("producto_nombre")
            or "Desconocido"
        )
        if reg:
            reg.estado = estado
            reg.producto = producto_nom
        else:
            reg = Pago(id_pago=str(payment_id), estado=estado, producto=producto_nom)
            db.session.add(reg)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        print("[webhook] error guardando pago:", e, flush=True)

    # Publicar a MQTT si aprobado
    try:
        if estado == "approved" and slot_id:
            mqtt_publish({
                "comando": "activar",
                "slot_id": slot_id,
                "pago_id": str(payment_id),
            })
            print("[webhook] MQTT publicado OK", flush=True)
    except Exception as e:
        print("[webhook] Error publicando MQTT:", e, flush=True)

    return jsonify({"status": "ok"}), 200

# ======================= Run local =======================
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=True)
