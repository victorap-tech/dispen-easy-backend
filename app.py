# app.py
import os
import json
from datetime import datetime

import requests
from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.sql import func

# MQTT
import paho.mqtt.client as mqtt

# --------------------------------
# Configuración básica
# --------------------------------
app = Flask(__name__)
CORS(app)

# Base de datos (PostgreSQL en Railway)
# Ej: postgresql://usuario:pass@host:puerto/db
DB_URL = os.getenv("DATABASE_URL", "sqlite:///./local.db")
app.config["SQLALCHEMY_DATABASE_URI"] = DB_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

# --------------------------------
# Mercado Pago
# --------------------------------
MP_TOKEN = os.getenv("MP_ACCESS_TOKEN", "")
MP_WEBHOOK_URL = os.getenv("MP_WEBHOOK_URL", "")  # https://<tu-backend>/api/mp/webhook

# --------------------------------
# MQTT
# --------------------------------
MQTT_HOST = os.getenv("MQTT_HOST", "")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USER = os.getenv("MQTT_USER", "")
MQTT_PASS = os.getenv("MQTT_PASS", "")
MQTT_ORDERS_TOPIC = os.getenv("MQTT_ORDERS_TOPIC", "dispense/orders")
MQTT_CLIENT_ID = os.getenv("MQTT_CLIENT_ID", "backend-dispense-easy")

mqttc = mqtt.Client(client_id=MQTT_CLIENT_ID)
if MQTT_USER:
    mqttc.username_pw_set(MQTT_USER, MQTT_PASS)

# TLS si usás 8883 (broker con SSL)
if MQTT_PORT == 8883:
    mqttc.tls_set()          # cert_reqs=ssl.CERT_REQUIRED por defecto
    mqttc.tls_insecure_set(False)


def _connect_mqtt():
    try:
        if MQTT_HOST:
            mqttc.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
            mqttc.loop_start()
            app.logger.info("MQTT conectado")
        else:
            app.logger.warning("MQTT deshabilitado (sin MQTT_HOST)")
    except Exception as e:
        app.logger.error(f"Error conectando MQTT: {e}")


# --------------------------------
# Modelos
# --------------------------------
class Producto(db.Model):
    __tablename__ = "producto"
    id         = db.Column(db.Integer, primary_key=True)
    nombre     = db.Column(db.String(100), nullable=False)
    precio     = db.Column(db.Float, nullable=False)      # ARS por operación (lo que cobrás)
    cantidad   = db.Column(db.Float, nullable=False)      # stock en litros
    slot_id    = db.Column(db.Integer, nullable=False)    # salida física (1..n)
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
    id            = db.Column(db.Integer, primary_key=True)
    mp_payment_id = db.Column(db.String(64), unique=True)  # id de MP
    product_id    = db.Column(db.Integer, db.ForeignKey("producto.id"), nullable=False)
    slot_id       = db.Column(db.Integer, nullable=False)
    monto         = db.Column(db.Float, nullable=False)     # ARS
    estado        = db.Column(db.String(20), nullable=False)  # approved|pending|rejected
    procesado     = db.Column(db.Boolean, nullable=False, default=False)
    created_at    = db.Column(db.DateTime(timezone=True), server_default=func.now())


class Reposicion(db.Model):
    __tablename__ = "reposicion"
    id         = db.Column(db.Integer, primary_key=True)
    producto_id= db.Column(db.Integer, db.ForeignKey("producto.id"), nullable=False)
    litros     = db.Column(db.Float, nullable=False)        # +X reponer, o diferencia en reset
    motivo     = db.Column(db.String(20), nullable=False)   # "reponer" | "reset" | "ajuste"
    created_at = db.Column(db.DateTime(timezone=True), server_default=func.now())


# --------------------------------
# Utils
# --------------------------------
def mp_headers():
    return {
        "Authorization": f"Bearer {MP_TOKEN}",
        "Content-Type": "application/json",
    }


def publicar_orden_mqtt(order_id: int, slot: int, product_id: int, amount: float):
    """Publica una orden de dispensado para el ESP vía MQTT.
       amount = litros a despachar.
    """
    if not MQTT_HOST:
        app.logger.warning("MQTT no configurado: no se publica orden.")
        return

    payload = {
        "order_id": order_id,
        "slot": slot,
        "product_id": product_id,
        "amount": float(amount),
        "ts": datetime.utcnow().isoformat() + "Z",
    }
    mqttc.publish(MQTT_ORDERS_TOPIC, json.dumps(payload), qos=1, retain=False)
    app.logger.info(f"[MQTT] Publicada orden {order_id} -> slot {slot}, litros={amount}")


# --------------------------------
# Rutas básicas
# --------------------------------
@app.get("/")
def home():
    return jsonify({"status": "ok", "message": "Backend Dispen-Easy operativo"})


# --------------------------------
# CRUD Productos
# --------------------------------
@app.get("/api/productos")
def productos_list():
    rows = Producto.query.order_by(Producto.id.asc()).all()
    return jsonify([r.to_dict() for r in rows])


@app.post("/api/productos")
def productos_create():
    data = request.get_json(force=True, silent=True) or {}
    nombre = (data.get("nombre") or "").strip()
    if not nombre:
        return jsonify({"ok": False, "error": "nombre requerido"}), 400

    p = Producto(
        nombre=nombre,
        precio=float(data.get("precio", 0)),
        cantidad=float(data.get("cantidad", 0)),
        slot_id=int(data.get("slot", data.get("slot_id", 1))),
        habilitado=bool(data.get("habilitado", False)),
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
    if "habilitado" in d:p.habilitado = bool(d["habilitado"])

    db.session.commit()
    return jsonify({"ok": True, "producto": p.to_dict()})


@app.delete("/api/productos/<int:pid>")
def productos_delete(pid):
    p = Producto.query.get_or_404(pid)
    db.session.delete(p)
    db.session.commit()
    return jsonify({"ok": True}), 204


# --------------------------------
# Reposición / Reset stock / Historial
# --------------------------------
@app.post("/api/productos/<int:pid>/reponer")
def reponer(pid):
    p = Producto.query.get_or_404(pid)
    data = request.get_json(force=True) or {}
    litros = float(data.get("litros", 0))
    if litros <= 0:
        return jsonify({"ok": False, "error": "litros debe ser > 0"}), 400

    p.cantidad += litros
    db.session.add(Reposicion(producto_id=pid, litros=litros, motivo="reponer"))
    db.session.commit()
    return jsonify({"ok": True, "producto": p.to_dict()})


@app.post("/api/productos/<int:pid>/reset_stock")
def reset_stock(pid):
    p = Producto.query.get_or_404(pid)
    data = request.get_json(force=True) or {}
    nuevo = float(data.get("litros", 0))
    diff = nuevo - p.cantidad
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
    return jsonify([{
        "id": r.id, "litros": r.litros, "motivo": r.motivo,
        "created_at": r.created_at.isoformat() if r.created_at else None
    } for r in rows])


# --------------------------------
# Mercado Pago: crear preferencia
# external_reference = "product_id:slot_id"
# metadata = { product_id, slot_id, litros }
# --------------------------------
@app.post("/api/pagos/preferencia")
def crear_preferencia():
    if not MP_TOKEN:
        return jsonify({"ok": False, "error": "MP_ACCESS_TOKEN no configurado"}), 500

    data = request.get_json(force=True) or {}
    product_id = int(data.get("product_id", 0))
    slot_id    = int(data.get("slot_id", 0))
    litros     = int(data.get("litros", 1))  # presentación (1,2,3, ... litros)
    prod = Producto.query.get_or_404(product_id)

    pref_body = {
        "items": [{
            "title": prod.nombre,
            "quantity": 1,
            "currency_id": "ARS",
            "unit_price": float(prod.precio),
        }],
        "back_urls": {
            "success": os.getenv("MP_BACK_SUCCESS", "https://www.mercadopago.com.ar"),
            "failure": os.getenv("MP_BACK_FAILURE", "https://www.mercadopago.com.ar"),
            "pending": os.getenv("MP_BACK_PENDING", "https://www.mercadopago.com.ar"),
        },
        "auto_return": "approved",
        "external_reference": f"{product_id}:{slot_id}",
        "metadata": { "product_id": product_id, "slot_id": slot_id, "litros": litros },
    }

    if MP_WEBHOOK_URL:
        pref_body["notification_url"] = MP_WEBHOOK_URL

    r = requests.post(
        "https://api.mercadopago.com/checkout/preferences",
        headers=mp_headers(),
        json=pref_body, timeout=15
    )
    r.raise_for_status()
    pref = r.json()
    return jsonify({
        "ok": True,
        "pref_id": pref.get("id"),
        "init_point": pref.get("init_point"),
        "sandbox_init_point": pref.get("sandbox_init_point"),
    })


# --------------------------------
# Webhook Mercado Pago
# - registra pago approved
# - descuenta stock (litros)
# - publica orden MQTT (slot)
# --------------------------------
@app.post("/api/mp/webhook")
def mp_webhook():
    try:
        # --- leer datos que pueden venir por query o por body ---
        args = request.args or {}
        body = request.get_json(silent=True) or {}

        # MP puede mandar: ?type=payment&id=...  ó  ?topic=merchant_order&id=...
        topic = (args.get("topic") or args.get("type") or body.get("type") or "").lower()
        payment_id = args.get("id")  # si topic/type = payment
        merchant_order_id = args.get("id") if topic == "merchant_order" else None

        # live_mode (True = producción, False = sandbox)
        live_mode = bool(body.get("live_mode", True))
        base_api = "https://api.mercadopago.com" if live_mode else "https://api.sandbox.mercadopago.com"

        app.logger.info(f"[MP] webhook topic={topic} live_mode={live_mode} args={dict(args)}")

        # Si vino merchant_order, obtener el/los payments primero
        if topic == "merchant_order" and merchant_order_id:
            r_mo = requests.get(
                f"{base_api}/merchant_orders/{merchant_order_id}",
                headers=mp_headers(),
                timeout=15,
            )
            r_mo.raise_for_status()
            mo = r_mo.json()
            pays = mo.get("payments") or []
            if not pays:
                app.logger.warning(f"[MP] merchant_order {merchant_order_id} sin payments")
                return "ok", 200
            payment_id = str(pays[0].get("id"))

        # Si aún no tenemos payment_id, intentar desde body.data.id
        if not payment_id:
            data = body.get("data") or {}
            payment_id = data.get("id")
        if not payment_id:
            app.logger.warning("[MP] webhook sin payment_id/merchant_order_id")
            return "ok", 200

        # --- buscar el pago en el API correcto ---
        app.logger.info(f"[MP] consultando payment_id={payment_id} api={base_api}")
        r = requests.get(
            f"{base_api}/v1/payments/{payment_id}",
            headers=mp_headers(),
            timeout=15,
        )
        r.raise_for_status()
        pago_mp = r.json()
        status = (pago_mp.get("status") or "").lower()
        if status != "approved":
            app.logger.info(f"[MP] payment {payment_id} status={status} (no se procesa)")
            return "ok", 200

        # --- idempotencia por mp_payment_id ---
        ya = Pago.query.filter_by(mp_payment_id=str(payment_id)).first()
        if ya:
            return "ok", 200

        # product_id / slot_id: primero external_reference "prod:slot",
        # fallback a metadata {product_id, slot_id}
        ext = pago_mp.get("external_reference") or ""
        try:
            product_id, slot_id = [int(x) for x in ext.split(":")]
        except Exception:
            md = pago_mp.get("metadata") or {}
            product_id = int(md.get("product_id", 0))
            slot_id = int(md.get("slot_id", 0))

        prod = Producto.query.get(product_id)
        monto = float(pago_mp.get("transaction_amount", prod.precio if prod else 0.0))

        # registrar pago
        reg = Pago(
            mp_payment_id=str(payment_id),
            product_id=product_id,
            slot_id=slot_id,
            monto=monto,
            estado="approved",
            procesado=False,
        )
        db.session.add(reg)

        # descontar 1L (ajusta a tu lógica si vendés por otra unidad)
        if prod and prod.cantidad > 0:
            prod.cantidad = max(0.0, float(prod.cantidad) - 1.0)

        db.session.commit()

        # publicar orden al ESP por MQTT (si está configurado)
        publicar_orden_mqtt(
            order_id=reg.id,
            slot=slot_id,
            product_id=product_id,
            amount=monto,
        )

        return "ok", 200

    except requests.HTTPError as e:
        # Log con URL para diagnosticar 404 de ambiente/token
        try:
            url = e.request.url  # puede no existir
        except Exception:
            url = "?"
        app.logger.error(f"[MP] HTTPError: {e} url={url}")
        return "ok", 200
    except Exception as e:
        app.logger.exception(f"[MP] Error webhook: {e}")
        return "ok", 200


# --------------------------------
# ACK opcional desde el ESP por HTTP
# (si no querés escuchar ACKs por MQTT)
# --------------------------------
@app.post("/api/dispense/ack/<int:order_id>")
def ack(order_id):
    g = Pago.query.get_or_404(order_id)
    if not g.procesado:
        g.procesado = True
        db.session.commit()
    return jsonify({"ok": True})


# --------------------------------
# Inicialización
# --------------------------------
def initialize_database():
    with app.app_context():
        db.create_all()
        app.logger.info("Tablas verificadas/creadas.")


# --------------------------------
# Entry point
# --------------------------------
if __name__ == "__main__":
    initialize_database()
    _connect_mqtt()
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
