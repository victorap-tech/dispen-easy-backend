import os
import io
import json
import base64
from datetime import datetime
from typing import Optional

import requests
from flask import Flask, jsonify, request
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from sqlalchemy import text, inspect
import qrcode

# -----------------------------
# Configuración base del server
# -----------------------------
app = Flask(__name__)

# Base de datos
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///dispen_easy.db")
app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

# CORS para todo lo que cuelgue de /api/*
CORS(app, resources={r"/api/*": {"origins": "*"}})


# -----------------------------
# Modelos
# -----------------------------
class Producto(db.Model):
    __tablename__ = "producto"
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(120), nullable=False)
    precio = db.Column(db.Float, nullable=False)  # en la moneda configurada en MP
    slot_id = db.Column(db.Integer, nullable=False, default=0)  # 0..5
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "nombre": self.nombre,
            "precio": self.precio,
            "slot_id": self.slot_id,
            "created_at": self.created_at.isoformat(),
        }


class Pago(db.Model):
    __tablename__ = "pago"
    id = db.Column(db.Integer, primary_key=True)
    mp_payment_id = db.Column(db.String(64), index=True)  # ID devuelto por MP
    external_reference = db.Column(db.String(128), nullable=True)
    status = db.Column(db.String(32), index=True)  # approved, rejected, etc.
    producto_id = db.Column(db.Integer, nullable=True)
    producto_nombre = db.Column(db.String(120), nullable=True)
    slot_id = db.Column(db.Integer, nullable=True)
    monto = db.Column(db.Float, nullable=True)
    dispensado = db.Column(db.Boolean, default=False, index=True)
    raw = db.Column(db.Text, nullable=True)  # json crudo por si hace falta auditar
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "mp_payment_id": self.mp_payment_id,
            "external_reference": self.external_reference,
            "status": self.status,
            "producto_id": self.producto_id,
            "producto_nombre": self.producto_nombre,
            "slot_id": self.slot_id,
            "monto": self.monto,
            "dispensado": self.dispensado,
            "created_at": self.created_at.isoformat(),
        }


# -----------------------------
# Utilitarios DB
# -----------------------------
def ensure_slot_id_column():
    """
    Asegura que exista la columna slot_id en la tabla producto de forma portable
    (útil si migraste desde una DB vieja).
    """
    try:
        engine = db.engine
        insp = inspect(engine)
        cols = [c["name"] for c in insp.get_columns("producto")]
        if "slot_id" not in cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE producto ADD COLUMN slot_id INTEGER"))
                conn.execute(text("UPDATE producto SET slot_id = 0 WHERE slot_id IS NULL"))
                try:
                    # En Postgres:
                    conn.execute(text("ALTER TABLE producto ALTER COLUMN slot_id SET DEFAULT 0"))
                    conn.execute(text("ALTER TABLE producto ALTER COLUMN slot_id SET NOT NULL"))
                except Exception as e:
                    print("[DB] No se pudo fijar NOT NULL/DEFAULT (continuo):", e)
            print("[DB] Columna slot_id agregada")
        else:
            print("[DB] Columna slot_id ya existe")
    except Exception as e:
        print("[DB] Error creando columna slot_id:", e)
        db.session.rollback()


def init_db():
    db.create_all()
    ensure_slot_id_column()


# -----------------------------
# MQTT (publicación simple por evento)
# -----------------------------
def mqtt_publish(payload: dict, topic: Optional[str] = None):
    """
    Publica un mensaje MQTT conectando, publicando y desconectando (simple y robusto).
    Si necesitás alto volumen, migrá a un cliente persistente.
    """
    import paho.mqtt.client as mqtt

    broker = os.getenv("MQTT_BROKER", "broker.hivemq.com")
    port = int(os.getenv("MQTT_PORT", "1883"))
    user = os.getenv("MQTT_USER", "")
    pwd = os.getenv("MQTT_PASS", "")
    client_id = os.getenv("MQTT_CLIENT_ID", "dispen-easy-backend")
    keepalive = int(os.getenv("MQTT_KEEPALIVE", "60"))
    topic = topic or os.getenv("MQTT_TOPIC", "dispen-easy/dispensar")

    client = mqtt.Client(client_id=client_id, protocol=mqtt.MQTTv311)

    if user:
        client.username_pw_set(user, pwd)

    # Nota: Si usás TLS (8883), descomentá estas líneas y exportá MQTT_PORT=8883
    # import ssl
    # client.tls_set(cert_reqs=ssl.CERT_NONE)
    # client.tls_insecure_set(True)

    client.connect(broker, port, keepalive)
    client.loop_start()
    payload_str = json.dumps(payload, ensure_ascii=False)
    result = client.publish(topic, payload_str, qos=1, retain=False)
    result.wait_for_publish()
    client.loop_stop()
    client.disconnect()
    print("[MQTT] Enviado a", topic, ":", payload_str, flush=True)


# -----------------------------
# Mercado Pago helpers
# -----------------------------
MP_API_BASE = "https://api.mercadopago.com"

def mp_headers():
    token = os.getenv("MP_ACCESS_TOKEN")
    if not token:
        raise RuntimeError("MP_ACCESS_TOKEN no configurado")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def mp_get_payment(payment_id: str):
    """
    Obtiene un pago por ID (recomendado en el webhook).
    """
    url = f"{MP_API_BASE}/v1/payments/{payment_id}"
    resp = requests.get(url, headers=mp_headers(), timeout=12)
    if resp.status_code != 200:
        raise RuntimeError(f"MP get payment {payment_id} fallo: {resp.status_code} {resp.text}")
    return resp.json()


# -----------------------------
# Endpoints de Productos (CRUD)
# -----------------------------
@app.route("/api/productos", methods=["GET"])
def listar_productos():
    productos = Producto.query.order_by(Producto.id.asc()).all()
    return jsonify([p.to_dict() for p in productos]), 200


@app.route("/api/productos", methods=["POST"])
def crear_producto():
    data = request.get_json() or {}
    nombre = (data.get("nombre") or "").strip()
    precio = data.get("precio")
    slot_id = data.get("slot_id")

    if not nombre:
        return jsonify({"error": "Falta nombre"}), 400
    try:
        precio = float(precio)
    except Exception:
        return jsonify({"error": "Precio inválido"}), 400

    # Asignación automática del primer slot libre 0..5 si no vino
    if slot_id is None:
        usados = {p.slot_id for p in Producto.query.all()}
        slot_id = None
        for i in range(6):
            if i not in usados:
                slot_id = i
                break
        if slot_id is None:
            return jsonify({"error": "No hay slots disponibles (0..5)"}), 400
    else:
        try:
            slot_id = int(slot_id)
            if slot_id < 0 or slot_id > 5:
                return jsonify({"error": "slot_id debe estar entre 0 y 5"}), 400
        except Exception:
            return jsonify({"error": "slot_id inválido"}), 400

    p = Producto(nombre=nombre, precio=precio, slot_id=slot_id)
    db.session.add(p)
    db.session.commit()
    return jsonify(p.to_dict()), 201


@app.route("/api/productos/<int:pid>", methods=["GET"])
def obtener_producto(pid):
    p = Producto.query.get(pid)
    if not p:
        return jsonify({"error": "Producto no encontrado"}), 404
    return jsonify(p.to_dict()), 200


@app.route("/api/productos/<int:pid>", methods=["PUT"])
def actualizar_producto(pid):
    p = Producto.query.get(pid)
    if not p:
        return jsonify({"error": "Producto no encontrado"}), 404

    data = request.get_json() or {}
    if "nombre" in data and data["nombre"] is not None:
        p.nombre = str(data["nombre"]).strip() or p.nombre
    if "precio" in data and data["precio"] is not None:
        try:
            p.precio = float(data["precio"])
        except Exception:
            return jsonify({"error": "Precio inválido"}), 400
    if "slot_id" in data and data["slot_id"] is not None:
        try:
            slot = int(data["slot_id"])
            if slot < 0 or slot > 5:
                return jsonify({"error": "slot_id debe estar entre 0 y 5"}), 400
            p.slot_id = slot
        except Exception:
            return jsonify({"error": "slot_id inválido"}), 400

    db.session.commit()
    return jsonify(p.to_dict()), 200


@app.route("/api/productos/<int:pid>", methods=["DELETE"])
def borrar_producto(pid):
    p = Producto.query.get(pid)
    if not p:
        return jsonify({"error": "Producto no encontrado"}), 404
    db.session.delete(p)
    db.session.commit()
    return jsonify({"status": "ok"}), 200


# -----------------------------
# Generar QR (link de pago)
# -----------------------------
@app.route("/api/generar_qr/<int:pid>", methods=["GET"])
def generar_qr(pid):
    producto = Producto.query.get(pid)
    if not producto:
        return jsonify({"error": "Producto no encontrado"}), 404

    try:
        token = os.getenv("MP_ACCESS_TOKEN")
        if not token:
            return jsonify({"error": "Falta MP_ACCESS_TOKEN"}), 500

        notification_url = os.getenv(
            "MP_NOTIFICATION_URL",
            # Cambiá esto por tu dominio en producción
            "https://web-production-e7d2.up.railway.app/webhook",
        )

        url = f"{MP_API_BASE}/checkout/preferences"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        payload = {
            "items": [{
                "title": producto.nombre,
                "quantity": 1,
                "unit_price": float(producto.precio),
            }],
            "description": producto.nombre,
            "additional_info": {"items": [{"title": producto.nombre}]},
            "metadata": {
                "producto_id": producto.id,           # clave alíneada con webhook
                "producto_nombre": producto.nombre,
                "slot_id": producto.slot_id,
            },
            "external_reference": f"prod:{producto.id}",
            "notification_url": notification_url,
            "back_urls": {
                "success": os.getenv("MP_BACK_URL_SUCCESS", "https://dispen-easy-web-production.up.railway.app/"),
                "pending": os.getenv("MP_BACK_URL_PENDING", "https://dispen-easy-web-production.up.railway.app/"),
                "failure": os.getenv("MP_BACK_URL_FAILURE", "https://dispen-easy-web-production.up.railway.app/"),
            },
            "auto_return": "approved",
        }

        resp = requests.post(url, headers=headers, json=payload, timeout=12)
        if resp.status_code != 201:
            try:
                detalle = resp.json()
            except Exception:
                detalle = resp.text
            print("MP error:", resp.status_code, detalle, flush=True)
            return jsonify({"error": "No se pudo generar link de pago", "detalle": detalle}), 502

        link = resp.json().get("init_point")

        img = qrcode.make(link)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        qr_base64 = base64.b64encode(buf.getvalue()).decode("utf-8")

        return jsonify({"qr_base64": qr_base64, "link": link}), 200

    except Exception as e:
        return jsonify({"error": "Error generando QR", "detalle": str(e)}), 500


# -----------------------------
# Webhook de Mercado Pago
# -----------------------------
@app.route("/webhook", methods=["POST", "GET"])
def webhook():
    """
    - Mercado Pago puede llamar con GET (verificación) y con POST (notificación real).
    - En POST, si viene type=payment e id, consultamos el pago para tener datos confiables.
    """
    if request.method == "GET":
        return jsonify({"status": "ok"}), 200

    try:
        data = request.get_json(silent=True) or {}
        # Notificaciones nuevas usan "type": "payment" y "data": {"id": "NNN"}
        notif_type = (data.get("type") or data.get("action") or "").lower()
        payment_id = None

        if notif_type == "payment":
            # formato nuevo
            payment_id = (data.get("data") or {}).get("id")
        else:
            # formato clásico: topic=payment & id=
            payment_id = data.get("id") or request.args.get("id")

        if not payment_id:
            return jsonify({"status": "ignored", "detalle": "sin payment_id"}), 200

        # Consultar el pago en MP para tener info confiable
        pay = mp_get_payment(str(payment_id))

        estado = (pay.get("status") or "").lower()
        meta = pay.get("metadata") or {}

        producto_nombre = (
            meta.get("producto_nombre")
            or pay.get("description")
            or ((pay.get("additional_info") or {}).get("items") or [{}])[0].get("title")
            or "Desconocido"
        )
        producto_id_real = meta.get("producto_id")
        slot_id = meta.get("slot_id")
        monto = None
        try:
            monto = float(pay.get("transaction_amount") or 0.0)
        except Exception:
            pass

        # Guardar/actualizar pago
        pago = Pago(
            mp_payment_id=str(payment_id),
            external_reference=pay.get("external_reference"),
            status=estado,
            producto_id=producto_id_real,
            producto_nombre=producto_nombre,
            slot_id=slot_id,
            monto=monto,
            dispensado=False,
            raw=json.dumps(pay, ensure_ascii=False),
        )
        db.session.add(pago)
        db.session.commit()

        # Si está aprobado, publicar a MQTT para activar dispensado
        if estado == "approved":
            mqtt_payload = {
                "comando": "activar",
                "slot_id": int(slot_id) if slot_id is not None else 0,
                "pago_id": str(payment_id),
                "mensaje": "Dispen-Easy activo"
            }
            try:
                mqtt_publish(mqtt_payload, topic=os.getenv("MQTT_TOPIC", "dispen-easy/dispensar"))
            except Exception as me:
                print("[MQTT] Error publicando:", me, flush=True)

        return jsonify({"status": "ok"}), 200

    except Exception as e:
        print("[WEBHOOK] Error:", e, flush=True)
        return jsonify({"status": "error", "detalle": str(e)}), 500


# -----------------------------
# Pagos pendientes / marcar dispensado
# -----------------------------
@app.route("/api/check_payment_pendiente", methods=["GET"])
def check_payment_pendiente():
    """
    Devuelve el último pago aprobado que aún no se marcó como dispensado.
    El ESP32 puede consultar esto en modo 'pull' si no está usando MQTT.
    """
    pago = (
        Pago.query.filter_by(status="approved", dispensado=False)
        .order_by(Pago.created_at.desc())
        .first()
    )
    if not pago:
        return jsonify({"pendiente": False}), 200
    return jsonify({"pendiente": True, "pago": pago.to_dict()}), 200


@app.route("/api/marcar_dispensado", methods=["POST"])
def marcar_dispensado():
    """
    Marca un pago como dispensado. Se puede llamar desde el ESP32
    tras completar la entrega.
    Body JSON: { "pago_id": "..." }  (puede ser mp_payment_id o id interno)
    """
    data = request.get_json() or {}
    pago_id = str(data.get("pago_id", "")).strip()
    if not pago_id:
        return jsonify({"error": "Falta pago_id"}), 400

    # Buscar por mp_payment_id primero, si no por id interno
    pago = Pago.query.filter_by(mp_payment_id=pago_id).first()
    if not pago and pago_id.isdigit():
        pago = Pago.query.get(int(pago_id))

    if not pago:
        return jsonify({"error": "Pago no encontrado"}), 404

    pago.dispensado = True
    db.session.commit()
    return jsonify({"status": "ok"}), 200


# -----------------------------
# Endpoint de prueba MQTT
# -----------------------------
@app.route("/api/test-mqtt", methods=["POST"])
def test_mqtt():
    try:
        data = request.get_json() or {}
        slot_id = int(data.get("slot_id", 0))
        pago_id = str(data.get("pago_id", "test123"))

        mqtt_payload = {
            "comando": "activar",
            "slot_id": slot_id,
            "pago_id": pago_id,
            "mensaje": "Dispen-Easy activo"
        }

        mqtt_publish(mqtt_payload, topic=os.getenv("MQTT_TOPIC", "dispen-easy/dispensar"))
        return jsonify({"status": "ok", "mensaje": "MQTT enviado", "payload": mqtt_payload}), 200
    except Exception as e:
        return jsonify({"status": "error", "detalle": str(e)}), 500


# -----------------------------
# Healthcheck
# -----------------------------
@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


# -----------------------------
# Boot
# -----------------------------
if __name__ == "__main__":
    # Inicializar DB
    with app.app_context():
        init_db()

    port = int(os.getenv("PORT", "5000"))
    debug = os.getenv("FLASK_DEBUG", "1") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug)
