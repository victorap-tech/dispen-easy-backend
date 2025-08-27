# app.py
import os
import json
from datetime import datetime

from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.sql import func
from sqlalchemy import text
import requests


# MQTT (opcional)
import paho.mqtt.client as mqtt


# -------------------------------
# Configuración básica
# -------------------------------
app = Flask(__name__)
CORS(app)

DB_URL = (
    os.getenv("SQLALCHEMY_DATABASE_URI")
    or os.getenv("DATABASE_URL")
    or "sqlite:////tmp/local.db"
)
app.config["SQLALCHEMY_DATABASE_URI"] = DB_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

# -------------------------------
# Mercado Pago
# -------------------------------
MP_TOKEN = os.getenv("MP_ACCESS_TOKEN", "")
MP_WEBHOOK_URL = os.getenv("MP_WEBHOOK_URL", "")

def mp_headers():
    return {
        "Authorization": f"Bearer {MP_TOKEN}",
        "Content-Type": "application/json",
    }


# -------------------------------
# MQTT (opcional)
# -------------------------------
MQTT_HOST = os.getenv("MQTT_HOST", "")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USER = os.getenv("MQTT_USER", "")
MQTT_PASS = os.getenv("MQTT_PASS", "")
MQTT_CLIENT_ID = os.getenv("MQTT_CLIENT_ID", "backend-dispen-easy")
MQTT_ORDERS_TOPIC = os.getenv("MQTT_ORDERS_TOPIC", "dispense/orders")

mqttc = mqtt.Client(client_id=MQTT_CLIENT_ID)
if MQTT_USER:
    mqttc.username_pw_set(MQTT_USER, MQTT_PASS)
if MQTT_PORT == 8883:
    mqttc.tls_set()
    mqttc.tls_insecure_set(False)

def _connect_mqtt():
    try:
        if MQTT_HOST:
            mqttc.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
            mqttc.loop_start()
            app.logger.info("[MQTT] conectado")
        else:
            app.logger.warning("MQTT deshabilitado (sin MQTT_HOST)")
    except Exception as e:
        app.logger.error(f"Error conectando MQTT: {e}")

def publicar_orden_mqtt(order_id:int, slot:int, product_id:int, amount:float):
    """Publica una orden de dispensado para el ESP vía MQTT."""
    if not MQTT_HOST:
        app.logger.warning("MQTT no configurado: no se publica orden.")
        return
    payload = {
        "order_id": order_id,
        "slot": slot,
        "product_id": product_id,
        "amount": amount,
        "ts": datetime.utcnow().isoformat() + "Z",
    }
    mqttc.publish(MQTT_ORDERS_TOPIC, json.dumps(payload), qos=1, retain=False)
    app.logger.info(f"[MQTT] Publicada orden {order_id} -> slot {slot}")


# -------------------------------
# Modelos
# -------------------------------
class Producto(db.Model):
    __tablename__ = "producto"

    id         = db.Column(db.Integer, primary_key=True)
    nombre     = db.Column(db.String(100), nullable=False)
    precio     = db.Column(db.Float, nullable=False)   # ARS
    cantidad   = db.Column(db.Float, nullable=False)   # litros disponibles
    slot_id    = db.Column(db.Integer, nullable=False) # salida física (1..6)
    habilitado = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime(timezone=True), server_default=func.now())
    updated_at = db.Column(db.DateTime(timezone=True), onupdate=func.now(), server_default=func.now())

    def to_dict(self):
        return {
            "id": self.id,
            "nombre": self.nombre,
            "precio": self.precio,
            "cantidad": self.cantidad,
            "slot": self.slot_id,
            "habilitado": self.habilitado,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class Pago(db.Model):
    __tablename__ = "pago"

    id = db.Column(db.Integer, primary_key=True)
    mp_payment_id = db.Column(db.String(120), unique=True, nullable=False)
    estado = db.Column(db.String(80), nullable=False)
    producto = db.Column(db.String(120), nullable=False)
    dispensado = db.Column(db.Boolean, default=False, nullable=False)  # 👈 AÑADIR
    slot_id = db.Column(db.Integer, nullable=True)
    monto = db.Column(db.Float, nullable=True)
    raw = db.Column(db.JSON, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now())
    product_id = db.Column(db.Integer, db.ForeignKey("producto.id"), nullable=True)

    def to_dict(self):
        return {
            "id": self.id,
            "mp_payment_id": self.mp_payment_id,
            "estado": self.estado,
            "producto": self.producto,
            "procesado": self.procesado,
            "slot_id": self.slot_id,
            "monto": self.monto,
            "product_id": self.product_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class Reposicion(db.Model):
    __tablename__ = "reposicion"

    id         = db.Column(db.Integer, primary_key=True)
    producto_id= db.Column(db.Integer, db.ForeignKey("producto.id"), nullable=False)
    litros     = db.Column(db.Float, nullable=False)   # +X reponer, o diferencia en reset
    motivo     = db.Column(db.String(20), nullable=False)  # "reponer" | "reset" | "ajuste"
    created_at = db.Column(db.DateTime(timezone=True), server_default=func.now())


# -------------------------------
# Rutas básicas
# -------------------------------
@app.get("/")
def home():
    return jsonify({"status": "ok", "message": "Backend Dispen-Easy operativo"})


# -------------------------------
# CRUD Productos
# -------------------------------
@app.get("/api/productos")
def productos_list():
    rows = Producto.query.order_by(Producto.id.asc()).all()
    return jsonify([r.to_dict() for r in rows])

@app.post("/api/productos")
def productos_create():
    d = request.get_json(force=True, silent=True) or {}
    nombre = (d.get("nombre") or "").strip()
    if not nombre:
        return jsonify({"ok": False, "error": "nombre requerido"}), 400

    p = Producto(
        nombre=nombre,
        precio=float(d.get("precio", 0)),
        cantidad=float(d.get("cantidad", 0)),
        slot_id=int(d.get("slot", d.get("slot_id", 1))),
        habilitado=bool(d.get("habilitado", False)),
    )
    db.session.add(p)
    db.session.commit()
    return jsonify({"ok": True, "producto": p.to_dict()}), 201

@app.put("/api/productos/<int:pid>")
def productos_update(pid):
    p = Producto.query.get_or_404(pid)
    d = request.get_json(force=True, silent=True) or {}

    if "nombre" in d:    p.nombre = str(d["nombre"]).strip()
    if "precio" in d:    p.precio = float(d["precio"])
    if "cantidad" in d:  p.cantidad = float(d["cantidad"])
    if "slot" in d:      p.slot_id = int(d["slot"])
    if "slot_id" in d:   p.slot_id = int(d["slot_id"])
    if "habilitado" in d: p.habilitado = bool(d["habilitado"])

    db.session.commit()
    return jsonify({"ok": True, "producto": p.to_dict()})

@app.delete("/api/productos/<int:pid>")
def productos_delete(pid):
    p = Producto.query.get_or_404(pid)
    db.session.delete(p)
    db.session.commit()
    return jsonify({"ok": True}), 204


# -------------------------------
# Reposición / Reset stock / Historial
# -------------------------------
@app.post("/api/productos/<int:pid>/reponer")
def reponer(pid):
    p = Producto.query.get_or_404(pid)
    d = request.get_json(force=True, silent=True) or {}
    litros = float(d.get("litros", 0))
    if litros <= 0:
        return jsonify({"ok": False, "error": "litros debe ser > 0"}), 400
    p.cantidad = float(p.cantidad) + litros
    db.session.add(Reposicion(producto_id=pid, litros=litros, motivo="reponer"))
    db.session.commit()
    return jsonify({"ok": True, "producto": p.to_dict()})

@app.post("/api/productos/<int:pid>/reset_stock")
def reset_stock(pid):
    p = Producto.query.get_or_404(pid)
    d = request.get_json(force=True, silent=True) or {}
    nuevo = float(d.get("litros", 0))
    diff = nuevo - float(p.cantidad)
    p.cantidad = nuevo
    db.session.add(Reposicion(producto_id=pid, litros=diff, motivo="reset"))
    db.session.commit()
    return jsonify({"ok": True, "producto": p.to_dict()})

@app.get("/api/productos/<int:pid>/reposiciones")
def ver_repos(pid):
    rows = (
        Reposicion.query
        .filter_by(producto_id=pid)
        .order_by(Reposicion.created_at.desc())
        .all()
    )
    return jsonify([
        {
            "id": r.id,
            "litros": r.litros,
            "motivo": r.motivo,
            "created_at": r.created_at.isoformat() if r.created_at else None
        } for r in rows
    ])


# -------------------------------
# Crear preferencia (Checkout Pro)
# -------------------------------
@app.post("/api/pagos/preferencia")
def crear_preferencia():
    if not MP_TOKEN:
        return jsonify({"ok": False, "error": "MP_ACCESS_TOKEN no configurado"}), 500

    d = request.get_json(force=True, silent=True) or {}
    product_id = int(d.get("product_id", 0))
    slot_id    = int(d.get("slot_id", 0))

    prod = Producto.query.get_or_404(product_id)

    pref_body = {
        "items": [{
            "title":   prod.nombre,
            "quantity": 1,
            "currency_id": "ARS",
            "unit_price": float(prod.precio),
        }],
        "back_urls": {
            "success": os.getenv("MP_BACK_SUCCESS", "https://www.mercadopago.com.ar"),
            "failure": os.getenv("MP_BACK_FAILURE", "https://www.mercadopago.com.ar"),
            "pending": os.getenv("MP_BACK_PENDING",  "https://www.mercadopago.com.ar"),
        },
        "auto_return": "approved",
        # external_reference facilita recuperar product/slot en el webhook
        "external_reference": f"{product_id}:{slot_id}",
        "metadata": { "product_id": product_id, "slot_id": slot_id },
    }
    if MP_WEBHOOK_URL:
        pref_body["notification_url"] = MP_WEBHOOK_URL

    r = requests.post(
        "https://api.mercadopago.com/checkout/preferences",
        headers=mp_headers(),
        json=pref_body,
        timeout=15,
    )
    r.raise_for_status()
    pref = r.json()
    return jsonify({
        "ok": True,
        "pref_id": pref.get("id"),
        "init_point": pref.get("init_point"),
        "sandbox_init_point": pref.get("sandbox_init_point"),
    })


# -------------------------------
# Webhook Mercado Pago
#  - registra pago "approved"
#  - descuenta stock (1 litro por defecto; ajusta según tu lógica)
#  - publica orden MQTT (slot)
# -------------------------------
@app.post("/api/mp/webhook")
def mp_webhook():
    # --- entradas posibles ---
    args = request.args or {}
    body = request.get_json(silent=True) or {}

    # topic/type puede venir en query o en body
    topic = (
        args.get("topic")
        or args.get("type")
        or body.get("type")
        or body.get("topic")
        or (body.get("action") or "").split(".")[0]
    )

    # base_api según live_mode (por si estás usando sandbox)
    live_mode = bool(body.get("live_mode", True))
    base_api = "https://api.mercadopago.com" if live_mode else "https://api.sandbox.mercadopago.com"

    app.logger.info(f"[MP] webhook topic={topic} live_mode={live_mode} args={dict(args)} body={body}")

    payment_id = None
    merchant_order_id = None

    # 1) querystring directo
    if (args.get("topic") == "payment" or args.get("type") == "payment") and args.get("id"):
        payment_id = str(args.get("id"))

    # 2) body clásico
    if not payment_id:
        pid = (body.get("data") or {}).get("id")
        if pid is not None:
            payment_id = str(pid)

    # 3) body legacy por "resource"
    if not payment_id:
        res = body.get("resource") or ""
        if "/v1/payments/" in res:
            payment_id = res.rstrip("/").split("/")[-1]

    # 4) merchant_order → hay que ir a buscar los payments
    if not payment_id:
        if (args.get("topic") == "merchant_order" or topic == "merchant_order"):
            merchant_order_id = str(args.get("id") or (body.get("data") or {}).get("id") or "")
        res = body.get("resource") or ""
        if not merchant_order_id and "/merchant_orders/" in res:
            merchant_order_id = res.rstrip("/").split("/")[-1]

        if merchant_order_id:
            try:
                r_mo = requests.get(
                    f"{base_api}/merchant_orders/{merchant_order_id}",
                    headers=mp_headers(),
                    timeout=15,
                )
                r_mo.raise_for_status()
                mo = r_mo.json()
                pays = mo.get("payments") or []
                if pays:
                    payment_id = str(pays[0].get("id"))
                else:
                    app.logger.warning(f"[MP] merchant_order {merchant_order_id} sin payments")
                    return "ok", 200
            except Exception as e:
                app.logger.exception(f"[MP] Error obteniendo merchant_order {merchant_order_id}: {e}")
                return "ok", 200

    if not payment_id:
        app.logger.warning("[MP] No se encontró payment_id en la notificación")
        return "ok", 200

    # --- obtener el pago ---
    try:
        r = requests.get(
            f"{base_api}/v1/payments/{payment_id}",
            headers=mp_headers(),
            timeout=15,
        )
        r.raise_for_status()
        pago_mp = r.json()
    except requests.HTTPError as e:
        app.logger.error(f"[MP] HTTPError: {e} - url: {base_api}/v1/payments/{payment_id}")
        return "ok", 200
    except Exception as e:
        app.logger.exception(f"[MP] Error consultando pago {payment_id}: {e}")
        return "ok", 200

    status = pago_mp.get("status")
    if status != "approved":
        app.logger.info(f"[MP] payment {payment_id} status={status} (no se procesa)")
        return "ok", 200

    # --- idempotencia: ya está en la tabla? ---
    ya = Pago.query.filter_by(mp_payment_id=str(payment_id)).first()
    if ya:
        app.logger.info(f"[DB] payment {payment_id} ya existía (id={ya.id})")
        return "ok", 200

    # Intentar extraer datos útiles
    ext = pago_mp.get("external_reference") or ""
    product_id = None
    slot_id = None
    try:
        # si la external_reference viene como "producto:slot" (p.ej. "12:3")
        if ":" in ext:
            a, b = ext.split(":", 1)
            product_id = int(a) if a.isdigit() else None
            slot_id = int(b) if b.isdigit() else None
        elif ext.isdigit():
            product_id = int(ext)
    except Exception:
        pass

    if not product_id:
        md = pago_mp.get("metadata") or {}
        product_id = md.get("product_id")
        slot_id = slot_id or md.get("slot_id")

    # precio / monto
    monto = float(
        pago_mp.get("transaction_amount")
        or pago_mp.get("total_paid_amount")
        or 0.0
    )

    # nombre producto (si tenés el ID, lo buscamos)
    prod = Producto.query.get(product_id) if product_id else None

    # --- grabar pago ---
    reg = Pago(
        mp_payment_id=str(payment_id),
        estado="approved",
        producto=(prod.nombre if prod else (pago_mp.get("description") or ""))[:120],
        dispensado=False,
        slot_id=int(slot_id or 0),
        monto=monto,
        raw=pago_mp,
        product_id=(product_id if product_id else None),
    )
    db.session.add(reg)

    # descontar stock si corresponde
    if prod and prod.cantidad is not None:
        try:
            prod.cantidad = max(0.0, float(prod.cantidad) - 1.0)
        except Exception:
            pass

    db.session.commit()
    app.logger.info(f"[DB] payment {payment_id} insertado (pago_id={reg.id})")

    # Publicar orden al ESP sólo si configuraste MQTT
    if os.getenv("MQTT_HOST") and os.getenv("MQTT_TOPIC"):
        try:
            publicar_orden_mqtt(
                order_id=reg.id,
                slot=reg.slot_id or 0,
                product_id=product_id or 0,
                amount=monto,
            )
        except Exception as e:
            app.logger.warning(f"[MQTT] no configurado: no se publica orden. Detalle: {e}")

    return "ok", 200


# -------------------------------
# ACK opcional desde el ESP (HTTP)
# -------------------------------
@app.post("/api/dispense/ack/<int:order_id>")
def ack(order_id):
    g = Pago.query.get_or_404(order_id)
    if not g.procesado:
        g.procesado = True
        db.session.commit()
    return jsonify({"ok": True})


# -------------------------------
# Inicialización
# -------------------------------
def initialize_database():
    with app.app_context():
        db.create_all()
        app.logger.info("Tablas verificadas/creadas.")


# -------------------------------
# Entry point
# -------------------------------
if __name__ == "__main__":
    initialize_database()
    _connect_mqtt()
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
