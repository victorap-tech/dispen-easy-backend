# app.py
import os, json, time, math
from datetime import datetime, timezone
from urllib.parse import urlencode

from flask import Flask, request, jsonify, redirect, abort
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import func, UniqueConstraint
from sqlalchemy.exc import IntegrityError
import requests

# ---- Opcional MQTT para reenviar órdenes al ESP ----
MQTT_ENABLED = bool(os.getenv("MQTT_HOST"))
try:
    import paho.mqtt.client as mqtt  # type: ignore
except Exception:
    MQTT_ENABLED = False

# =====================================================
# Config Flask / DB
# =====================================================
app = Flask(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "").replace("postgres://", "postgresql://")
if not DATABASE_URL:
    raise RuntimeError("Falta DATABASE_URL")

app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

# =====================================================
# Modelos
# =====================================================
class Dispenser(db.Model):
    __tablename__ = "dispenser"
    id = db.Column(db.Integer, primary_key=True)
    device_id = db.Column(db.String(64), nullable=False, unique=True)   # ej: "dispen-01"
    nombre = db.Column(db.String(80), nullable=False, default="")
    activo = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=func.now())

class Producto(db.Model):
    __tablename__ = "producto"
    id = db.Column(db.Integer, primary_key=True)
    dispenser_id = db.Column(db.Integer, db.ForeignKey("dispenser.id", ondelete="SET NULL"), index=True, nullable=False)
    nombre = db.Column(db.String(100), nullable=False)
    precio = db.Column(db.Float, nullable=False)
    cantidad = db.Column(db.Integer, nullable=False)          # stock en litros (enteros)
    slot_id = db.Column(db.Integer, nullable=False)           # 1..6
    porcion_litros = db.Column(db.Integer, nullable=False, default=1)
    habilitado = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = db.Column(db.DateTime(timezone=True), onupdate=func.now())

    __table_args__ = (
        # Un slot pertenece a un solo producto dentro del mismo dispenser:
        UniqueConstraint("dispenser_id", "slot_id", name="uq_disp_slot"),
        db.Index("idx_producto_slot_id", "slot_id"),
    )

class Pago(db.Model):
    __tablename__ = "pago"
    id = db.Column(db.Integer, primary_key=True)
    mp_payment_id = db.Column(db.String(64), index=True)
    estado = db.Column(db.String(32), index=True)        # approved | pending | rejected
    monto = db.Column(db.Float)
    litros = db.Column(db.Integer)
    slot_id = db.Column(db.Integer)
    product_id = db.Column(db.Integer, db.ForeignKey("producto.id"))
    dispenser_id = db.Column(db.Integer, db.ForeignKey("dispenser.id"))
    dispensado = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, server_default=func.now())

with app.app_context():
    db.create_all()

# =====================================================
# Helpers & Config
# =====================================================
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "")

def require_admin():
    if not ADMIN_SECRET:
        return  # para pruebas locales
    sec = request.headers.get("x-admin-secret", "")
    if sec != ADMIN_SECRET:
        abort(401)

def env_int(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default

def now_iso():
    return datetime.now(timezone.utc).isoformat()

UMBRAL_ALERTA_LTS = env_int("UMBRAL_ALERTA_LTS", 3)
STOCK_RESERVA_LTS = env_int("STOCK_RESERVA_LTS", 2)

def get_mp_mode():
    return (os.getenv("MP_MODE", "test") or "test").lower()

def get_mp_token():
    mode = get_mp_mode()
    if mode == "live":
        return os.getenv("MP_ACCESS_TOKEN_LIVE", "")
    return os.getenv("MP_ACCESS_TOKEN_TEST", "")

# MQTT
mqtt_client = None
MQTT_TOPIC_PREFIX = os.getenv("MQTT_TOPIC_PREFIX", "dispen")
def mqtt_connect():
    global mqtt_client
    if not MQTT_ENABLED:
        return
    if mqtt_client:
        return
    mqtt_client = mqtt.Client()
    user = os.getenv("MQTT_USER")
    pwd = os.getenv("MQTT_PASS")
    if user:
        mqtt_client.username_pw_set(user, pwd or "")
    if os.getenv("MQTT_TLS", "insecure").lower() == "insecure":
        try:
            mqtt_client.tls_set()  # certific ado del sistema
        except Exception:
            pass
        mqtt_client.tls_insecure_set(True)
    host = os.getenv("MQTT_HOST")
    port = int(os.getenv("MQTT_PORT", "8883"))
    mqtt_client.connect(host, port, 30)
    mqtt_client.loop_start()

def mqtt_publish(device_id: str, slot_id: int, litros: int, timeout_s: int = 5):
    if not MQTT_ENABLED:
        return False
    try:
        mqtt_connect()
        topic = f"{MQTT_TOPIC_PREFIX}/{device_id}/cmd/dispense"
        payload = json.dumps({"payment_id": f"manual-{int(time.time())}",
                              "slot_id": int(slot_id),
                              "litros": int(litros),
                              "timeout_s": int(timeout_s)})
        mqtt_client.publish(topic, payload, qos=0, retain=False)
        return True
    except Exception:
        return False

# =====================================================
# Mercado Pago
# =====================================================
def mp_create_preference(title: str, qty_litros: int, unit_price: float, back_url_success: str, metadata: dict):
    token = get_mp_token()
    if not token:
        raise RuntimeError("Falta access token de MercadoPago")

    body = {
        "items": [{
            "title": title,
            "quantity": 1,
            "unit_price": round(unit_price, 2),
            "currency_id": "ARS",
            "description": f"{qty_litros} L",
        }],
        "back_urls": {
            "success": back_url_success,
            "pending": back_url_success,
            "failure": back_url_success,
        },
        "auto_return": "approved",
        "metadata": metadata or {},
    }

    r = requests.post(
        "https://api.mercadopago.com/checkout/preferences",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        data=json.dumps(body),
        timeout=20,
    )
    if r.status_code >= 300:
        raise RuntimeError(f"MP pref error {r.status_code}: {r.text}")

    data = r.json()
    link = data.get("init_point") or data.get("sandbox_init_point")
    return {"id": data.get("id"), "link": link or ""}

# =====================================================
# Serializadores
# =====================================================
def prod_json(p: Producto):
    return {
        "id": p.id,
        "dispenser_id": p.dispenser_id,
        "nombre": p.nombre,
        "precio": p.precio,
        "cantidad": p.cantidad,
        "slot": p.slot_id,
        "porcion_litros": p.porcion_litros,
        "habilitado": p.habilitado,
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }

def disp_json(d: Dispenser):
    return {"id": d.id, "device_id": d.device_id, "nombre": d.nombre, "activo": d.activo}

def pago_json(pg: Pago):
    return {
        "id": pg.id,
        "mp_payment_id": pg.mp_payment_id,
        "estado": pg.estado,
        "monto": pg.monto,
        "litros": pg.litros,
        "slot_id": pg.slot_id,
        "product_id": pg.product_id,
        "dispenser_id": pg.dispenser_id,
        "dispensado": pg.dispensado,
        "created_at": pg.created_at.isoformat() if pg.created_at else None,
    }

# =====================================================
# Rutas: Config
# =====================================================
@app.get("/api/config")
def api_config():
    require_admin()
    return jsonify({
        "mp_mode": get_mp_mode(),
        "umbral_alerta_lts": UMBRAL_ALERTA_LTS,
        "stock_reserva_lts": STOCK_RESERVA_LTS,
    })

@app.post("/api/mp/mode")
def api_set_mode():
    require_admin()
    mode = (request.json or {}).get("mode", "test").lower()
    if mode not in ("test", "live"):
        return jsonify({"error": "mode inválido"}), 400
    os.environ["MP_MODE"] = mode  # persiste en proceso
    return jsonify({"ok": True, "mode": mode})

# =====================================================
# Rutas: Dispensers
# =====================================================
@app.get("/api/dispensers")
def api_list_dispensers():
    require_admin()
    d = Dispenser.query.order_by(Dispenser.id.asc()).all()
    return jsonify([disp_json(x) for x in d])

@app.post("/api/dispensers")
def api_create_dispenser():
    require_admin()
    data = request.json or {}
    device_id = (data.get("device_id") or "").strip()
    nombre = (data.get("nombre") or "").strip() or device_id
    if not device_id:
        return jsonify({"error": "device_id requerido"}), 400
    d = Dispenser(device_id=device_id, nombre=nombre, activo=True)
    db.session.add(d)
    try:
        db.session.commit()
    except IntegrityError as e:
        db.session.rollback()
        return jsonify({"error": "device_id duplicado"}), 400
    return jsonify({"dispenser": disp_json(d)})

@app.put("/api/dispensers/<int:disp_id>")
def api_update_dispenser(disp_id):
    require_admin()
    d = Dispenser.query.get_or_404(disp_id)
    data = request.json or {}
    if "nombre" in data: d.nombre = str(data["nombre"] or "")
    if "activo" in data: d.activo = bool(data["activo"])
    db.session.commit()
    return jsonify({"dispenser": disp_json(d)})

# =====================================================
# Rutas: Productos
# =====================================================
@app.get("/api/productos")
def api_list_productos():
    require_admin()
    q = Producto.query
    disp_id = request.args.get("dispenser_id", type=int)
    if disp_id:
        q = q.filter(Producto.dispenser_id == disp_id)
    prods = q.order_by(Producto.dispenser_id.asc(), Producto.slot_id.asc()).all()
    return jsonify([prod_json(p) for p in prods])

@app.post("/api/productos")
def api_create_producto():
    require_admin()
    data = request.json or {}
    dispenser_id = int(data.get("dispenser_id") or 0)
    slot_id = int(data.get("slot") or 0)
    if not (1 <= slot_id <= 6):
        return jsonify({"error": "slot_id debe ser 1..6"}), 400
    if not dispenser_id:
        return jsonify({"error": "dispenser_id requerido"}), 400

    p = Producto(
        dispenser_id=dispenser_id,
        nombre=str(data.get("nombre") or ""),
        precio=float(data.get("precio") or 0),
        cantidad=int(data.get("cantidad") or 0),
        slot_id=slot_id,
        porcion_litros=max(1, int(data.get("porcion_litros") or 1)),
        habilitado=bool(data.get("habilitado") or False),
    )
    db.session.add(p)
    try:
        db.session.commit()
    except IntegrityError as e:
        db.session.rollback()
        # Puede ser violación de uq_disp_slot
        return jsonify({"error": "slot duplicado para ese dispenser", "detail": str(e)}), 500
    return jsonify({"producto": prod_json(p)})

@app.put("/api/productos/<int:pid>")
def api_update_producto(pid):
    require_admin()
    p = Producto.query.get_or_404(pid)
    data = request.json or {}
    if "nombre" in data: p.nombre = str(data["nombre"] or "")
    if "precio" in data: p.precio = float(data["precio"] or 0)
    if "cantidad" in data: p.cantidad = int(data["cantidad"] or 0)
    if "porcion_litros" in data: p.porcion_litros = max(1, int(data["porcion_litros"] or 1))
    if "habilitado" in data: p.habilitado = bool(data["habilitado"])
    if "slot" in data:
        new_slot = int(data["slot"] or 0)
        if not (1 <= new_slot <= 6):
            return jsonify({"error": "slot 1..6"}), 400
        p.slot_id = new_slot
    try:
        db.session.commit()
    except IntegrityError as e:
        db.session.rollback()
        return jsonify({"error": "conflicto de slot en el dispenser", "detail": str(e)}), 400
    return jsonify({"producto": prod_json(p)})

@app.delete("/api/productos/<int:pid>")
def api_delete_producto(pid):
    require_admin()
    p = Producto.query.get_or_404(pid)
    db.session.delete(p)
    db.session.commit()
    return "", 204

@app.post("/api/productos/<int:pid>/reponer")
def api_reponer_producto(pid):
    require_admin()
    litros = int((request.json or {}).get("litros") or 0)
    if litros <= 0:
        return jsonify({"error": "litros > 0"}), 400
    p = Producto.query.get_or_404(pid)
    p.cantidad = int(p.cantidad) + int(litros)
    db.session.commit()
    return jsonify({"producto": prod_json(p)})

@app.post("/api/productos/<int:pid>/reset_stock")
def api_reset_stock(pid):
    require_admin()
    litros = int((request.json or {}).get("litros") or 0)
    if litros < 0:
        return jsonify({"error": "litros >= 0"}), 400
    p = Producto.query.get_or_404(pid)
    p.cantidad = int(litros)
    db.session.commit()
    return jsonify({"producto": prod_json(p)})

# =====================================================
# Rutas: Pagos
# =====================================================
@app.get("/api/pagos")
def api_list_pagos():
    require_admin()
    q = Pago.query.order_by(Pago.id.desc())
    estado = request.args.get("estado")
    if estado:
        q = q.filter(Pago.estado == estado)
    qstr = request.args.get("q")
    if qstr:
        q = q.filter(Pago.mp_payment_id.ilike(f"%{qstr}%"))
    limit = request.args.get("limit", type=int) or 50
    return jsonify([pago_json(x) for x in q.limit(limit).all()])

@app.post("/api/pagos/preferencia")
def api_pago_preferencia():
    require_admin()
    data = request.json or {}
    pid = int(data.get("product_id") or 0)
    litros = int(data.get("litros") or 1)
    p = Producto.query.get_or_404(pid)

    link_info = mp_create_preference(
        title=p.nombre,
        qty_litros=litros,
        unit_price=p.precio * litros,
        back_url_success=f"{request.host_url.rstrip('/')}/gracias",
        metadata={"product_id": p.id, "dispenser_id": p.dispenser_id, "slot_id": p.slot_id, "litros": litros},
    )
    return jsonify({"link": link_info["link"], "pref_id": link_info["id"]})

@app.post("/api/pagos/<int:pgid>/reenviar")
def api_pago_reenviar(pgid):
    require_admin()
    pg = Pago.query.get_or_404(pgid)
    if pg.estado != "approved" or pg.dispensado is True:
        return jsonify({"error": "Solo pagos approved y no dispensados"}), 400
    disp = Dispenser.query.get(pg.dispenser_id)
    if not disp:
        return jsonify({"error": "Dispenser no encontrado"}), 404
    ok = mqtt_publish(disp.device_id, pg.slot_id, pg.litros, timeout_s=5)
    return jsonify({"ok": bool(ok), "msg": "Reenvío enviado" if ok else "No se pudo publicar MQTT"})

# =====================================================
# Front de compra por QR fijo (/go)
# =====================================================
def get_product_from_query():
    """
    Soporta:
      - /go?pid=78
      - /go?disp=2&slot=4
    """
    pid = request.args.get("pid", type=int)
    if pid:
        p = Producto.query.get(pid)
        return p

    disp = request.args.get("disp", type=int)
    slot = request.args.get("slot", type=int)
    if disp and slot:
        return Producto.query.filter(
            Producto.dispenser_id == disp,
            Producto.slot_id == slot
        ).first()
    return None

@app.get("/go")
def go_qr():
    p = get_product_from_query()
    if not p or not p.habilitado:
        return redirect(f"/sin_stock")  # podés servir una página estática simple

    litros = max(1, p.porcion_litros)
    # Bloqueo por reserva crítica: si vender litros dejaría <= STOCK_RESERVA_LTS → bloquear
    if (p.cantidad - litros) < STOCK_RESERVA_LTS:
        return redirect(f"/sin_stock")

    # Crea preferencia y redirige
    link_info = mp_create_preference(
        title=p.nombre,
        qty_litros=litros,
        unit_price=p.precio * litros,
        back_url_success=f"{request.host_url.rstrip('/')}/gracias",
        metadata={"product_id": p.id, "dispenser_id": p.dispenser_id, "slot_id": p.slot_id, "litros": litros},
    )
    return redirect(link_info["link"], code=302)

# =====================================================
# Seeds / Utilidad: crear 10 dispensers si no hay
# =====================================================
@app.post("/api/seed/dispensers10")
def seed_dispensers():
    require_admin()
    if Dispenser.query.count() > 0:
        return jsonify({"msg": "ya existen"}), 200
    items = []
    for i in range(1, 11):
        d = Dispenser(device_id=f"dispen-{i:02d}", nombre=f"Dispenser {i}", activo=True)
        db.session.add(d); items.append(d)
    db.session.commit()
    return jsonify({"created": [disp_json(x) for x in items]})

# =====================================================
# Healthcheck
# =====================================================
@app.get("/")
def root():
    return jsonify({"ok": True, "time": now_iso(), "mp_mode": get_mp_mode()})

# =====================================================
# Webhook de MP (opcional muy simple demo)
# =====================================================
@app.post("/webhooks/mp")
def mp_webhook():
    """
    Demo simple: cuando recibimos un pago approved, registramos Pago.
    (En producción deberías consultar el pago a MP por id)
    """
    data = request.json or {}
    # En notificaciones v1, suele venir "data": {"id": "..."} y "type": "payment"
    # Para simplificar, permitimos payload directo:
    mp_payment_id = str(data.get("mp_payment_id") or data.get("id") or "")
    estado = (data.get("estado") or data.get("status") or "").lower()
    metadata = data.get("metadata") or {}
    try:
        litros = int(metadata.get("litros") or data.get("litros") or 1)
    except Exception:
        litros = 1

    product_id = int(metadata.get("product_id") or data.get("product_id") or 0)
    dispenser_id = int(metadata.get("dispenser_id") or data.get("dispenser_id") or 0)
    slot_id = int(metadata.get("slot_id") or data.get("slot_id") or 0)
    monto = float(data.get("monto") or data.get("transaction_amount") or 0)

    pg = Pago(
        mp_payment_id=mp_payment_id, estado=estado, monto=monto, litros=litros,
        slot_id=slot_id, product_id=product_id, dispenser_id=dispenser_id,
        dispensado=False
    )
    db.session.add(pg)

    # Descontar stock si approved (optimista)
    if estado == "approved" and product_id:
        p = Producto.query.get(product_id)
        if p:
            p.cantidad = max(0, int(p.cantidad) - int(litros))
    db.session.commit()

    # Disparar MQTT al ESP si approved
    if estado == "approved" and dispenser_id and slot_id:
        disp = Dispenser.query.get(dispenser_id)
        if disp:
            mqtt_publish(disp.device_id, slot_id, litros, timeout_s=5)

    return jsonify({"ok": True})

# =====================================================
# Run
# =====================================================
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
