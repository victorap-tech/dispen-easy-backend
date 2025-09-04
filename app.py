# app.py
import os
import logging
import threading
import requests
import json as _json

from flask import Flask, jsonify, request
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from sqlalchemy.dialects.postgresql import JSONB
import paho.mqtt.client as mqtt

# -------------------------------------------------------------
# Config
# -------------------------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL", "")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# Tokens de MP (podés definir uno o ambos)
MP_ACCESS_TOKEN_LIVE = os.getenv("MP_ACCESS_TOKEN_LIVE", "").strip()
MP_ACCESS_TOKEN_TEST = os.getenv("MP_ACCESS_TOKEN_TEST", "").strip()
# Compat: si solo usás MP_ACCESS_TOKEN lo tomo como LIVE
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "").strip()
if MP_ACCESS_TOKEN and not MP_ACCESS_TOKEN_LIVE:
    MP_ACCESS_TOKEN_LIVE = MP_ACCESS_TOKEN

BACKEND_BASE_URL = (os.getenv("BACKEND_BASE_URL", "") or "").rstrip("/")
WEB_URL = os.getenv("WEB_URL", "https://example.com").strip()

MQTT_HOST = os.getenv("MQTT_HOST", "").strip()
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883") or 1883)
MQTT_USER = os.getenv("MQTT_USER", "")
MQTT_PASS = os.getenv("MQTT_PASS", "")
DEVICE_ID = os.getenv("DEVICE_ID", "dispen-01").strip()

# Seguridad Admin
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "").strip()
PUBLIC_PATHS = {
    "/",                          # health
    "/api/mp/webhook",            # MercadoPago webhook
    "/api/pagos/preferencia",     # generar link/QR
    "/api/pagos/pendiente",       # consulta del ESP32
}

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL or "sqlite:///local.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
CORS(
    app,
    resources={r"/api/*": {"origins": "*"}},
    allow_headers=["Content-Type", "x-admin-secret"],
    expose_headers=["Content-Type"],
)

db = SQLAlchemy(app)
logging.basicConfig(level=logging.INFO)
app.logger.setLevel(logging.INFO)

# -------------------------------------------------------------
# Modelos
# -------------------------------------------------------------
class KV(db.Model):
    __tablename__ = "kv"
    key = db.Column(db.String(80), primary_key=True)
    value = db.Column(db.String(400), nullable=False, default="")

class Producto(db.Model):
    __tablename__ = "producto"
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(100), nullable=False)
    precio = db.Column(db.Float, nullable=False)                # $ por litro
    cantidad = db.Column(db.Integer, nullable=False)            # stock disponible (L)
    slot_id = db.Column(db.Integer, nullable=False, unique=True, index=True)
    porcion_litros = db.Column(db.Integer, nullable=False, server_default="1")
    created_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now(), nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), onupdate=db.func.now())
    habilitado = db.Column(db.Boolean, nullable=False, server_default=db.text("false"))

class Pago(db.Model):
    __tablename__ = "pago"
    id = db.Column(db.Integer, primary_key=True)
    mp_payment_id = db.Column(db.String(120), nullable=False, unique=True, index=True)
    estado = db.Column(db.String(80), nullable=False)           # approved/pending/rejected
    producto = db.Column(db.String(120), nullable=False, default="")
    dispensado = db.Column(db.Boolean, nullable=False, server_default=db.text("false"))
    procesado = db.Column(db.Boolean, nullable=False, server_default=db.text("false"))  # idempotencia
    slot_id = db.Column(db.Integer, nullable=False, default=0)
    litros = db.Column(db.Integer, nullable=False, default=1)
    monto = db.Column(db.Integer, nullable=False, default=0)    # ARS entero
    product_id = db.Column(db.Integer, nullable=False, default=0)
    raw = db.Column(JSONB, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now(), nullable=False)

with app.app_context():
    db.create_all()
    # valor por defecto para el modo de pago
    if not KV.query.get("mp_mode"):
        db.session.add(KV(key="mp_mode", value="test"))  # "test" | "live"
        db.session.commit()

# -------------------------------------------------------------
# Helpers
# -------------------------------------------------------------
def ok_json(data, status=200): return jsonify(data), status

def json_error(msg, status=400, extra=None):
    payload = {"error": msg}
    if extra is not None: payload["detail"] = extra
    return jsonify(payload), status

def _to_int(x, default=0):
    try: return int(x)
    except Exception:
        try: return int(float(x))
        except Exception: return default

def serialize_producto(p: Producto) -> dict:
    return {
        "id": p.id, "nombre": p.nombre, "precio": float(p.precio),
        "cantidad": int(p.cantidad), "slot": int(p.slot_id),
        "porcion_litros": int(getattr(p, "porcion_litros", 1) or 1),
        "habilitado": bool(p.habilitado),
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }

def require_admin():
    if request.path in PUBLIC_PATHS:
        return None
    if not ADMIN_SECRET:
        return None
    if request.headers.get("x-admin-secret") != ADMIN_SECRET:
        return json_error("unauthorized", 401)

def get_mp_mode() -> str:
    kv = KV.query.get("mp_mode")
    return (kv.value if kv else "test").lower() in ("live", "prod", "production") and "live" or "test"

def get_mp_token_and_base() -> tuple[str, str]:
    mode = get_mp_mode()
    if mode == "live":
        token = MP_ACCESS_TOKEN_LIVE
        base_api = "https://api.mercadopago.com"
    else:
        # aunque usemos preferencia LIVE, podés forzar sandbox con token test
        token = MP_ACCESS_TOKEN_TEST or MP_ACCESS_TOKEN_LIVE
        base_api = "https://api.mercadopago.com"  # Payments v1 responde igual; live_mode vendrá en body
    return token, base_api

@app.before_request
def _auth_guard():
    resp = require_admin()
    if resp is not None:
        return resp

# -------------------------------------------------------------
# MQTT (publicar orden y procesar confirmación del ESP)
# -------------------------------------------------------------
MQTT_TOPIC_CMD    = f"dispen/{DEVICE_ID}/cmd/dispense"
MQTT_TOPIC_STATE  = f"dispen/{DEVICE_ID}/state/dispense"
MQTT_TOPIC_STATUS = f"dispen/{DEVICE_ID}/status"
_mqtt_client = None
_mqtt_lock = threading.Lock()

def _mqtt_on_connect(client, userdata, flags, rc, props=None):
    app.logger.info(f"[MQTT] conectado rc={rc}; subscribe {MQTT_TOPIC_STATE} y {MQTT_TOPIC_STATUS}")
    client.subscribe(MQTT_TOPIC_STATE, qos=1)
    client.subscribe(MQTT_TOPIC_STATUS, qos=1)

def _mqtt_on_message(client, userdata, msg):
    try: raw = msg.payload.decode("utf-8", "ignore")
    except Exception: raw = "<binario>"
    app.logger.info(f"[MQTT] RX topic={msg.topic} payload={raw}")

    try: data = _json.loads(raw or "{}")
    except Exception:
        app.logger.exception("[MQTT] payload inválido (no JSON)"); return

    payment_id = str(data.get("payment_id") or data.get("paymentId") or data.get("id") or "").strip()
    status = str(data.get("status") or data.get("state") or "").lower()
    slot_id = _to_int(data.get("slot_id") or data.get("slot") or 0)
    litros  = _to_int(data.get("litros") or data.get("liters") or 0)
    if status in ("ok", "finish", "finished", "success"): status = "done"

    if not payment_id or status not in ("done", "error", "timeout"): return

    with app.app_context():
        p = Pago.query.filter_by(mp_payment_id=payment_id).first()
        if not p: app.logger.warning(f"[MQTT] pago {payment_id} no encontrado"); return
        if status == "done" and not p.dispensado:
            try:
                litros_desc = int(p.litros or 0) or (litros or 1)
                prod = Producto.query.get(p.product_id) if p.product_id else None
                if prod: prod.cantidad = max(0, int(prod.cantidad or 0) - litros_desc)
                p.dispensado = True
                db.session.commit()
                app.logger.info(f"[MQTT] pago {payment_id} DISPENSADO; stock -{litros_desc}L (prod_id={p.product_id})")
            except Exception:
                db.session.rollback(); app.logger.exception("[MQTT] error al marcar dispensado/stock")

def start_mqtt_background():
    if not MQTT_HOST:
        app.logger.warning("[MQTT] MQTT_HOST no configurado; no se inicia MQTT"); return
    def _run():
        global _mqtt_client
        _mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"{DEVICE_ID}-backend")
        if MQTT_USER or MQTT_PASS: _mqtt_client.username_pw_set(MQTT_USER, MQTT_PASS)
        if int(MQTT_PORT) == 8883:
            try: _mqtt_client.tls_set()
            except Exception as e: app.logger.error(f"[MQTT] No se pudo habilitar TLS: {e}")
        _mqtt_client.on_connect = _mqtt_on_connect
        _mqtt_client.on_message = _mqtt_on_message
        _mqtt_client.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
        _mqtt_client.loop_forever()
    threading.Thread(target=_run, name="mqtt-thread", daemon=True).start()

def send_dispense_cmd(payment_id: str, slot_id: int, litros: int, timeout_s: int = 30) -> bool:
    if not MQTT_HOST:
        app.logger.warning("[MQTT] sin host; no se publica"); return False
    msg = {"payment_id": str(payment_id), "slot_id": int(slot_id), "litros": int(litros or 1), "timeout_s": int(timeout_s or 30)}
    payload = _json.dumps(msg, ensure_ascii=False)
    with _mqtt_lock:
        if not _mqtt_client:
            app.logger.warning("[MQTT] cliente no inicializado"); return False
        info = _mqtt_client.publish(MQTT_TOPIC_CMD, payload, qos=1, retain=False)
        ok = info.rc == mqtt.MQTT_ERR_SUCCESS
        app.logger.info(f"[MQTT] publish {MQTT_TOPIC_CMD} ok={ok} payload={payload}")
        return ok

# -------------------------------------------------------------
# Health
# -------------------------------------------------------------
@app.get("/")
def health():
    return ok_json({"status": "ok", "message": "Backend Dispen-Easy operativo", "mp_mode": get_mp_mode()})

# -------------------------------------------------------------
# Config (modo de pago)
# -------------------------------------------------------------
@app.get("/api/config")
def api_get_config():
    return ok_json({"mp_mode": get_mp_mode()})

@app.post("/api/mp/mode")
def api_set_mode():
    data = request.get_json(force=True, silent=True) or {}
    mode = str(data.get("mode") or "").lower()
    if mode not in ("test", "live"): return json_error("mode inválido (test|live)", 400)
    kv = KV.query.get("mp_mode")
    if not kv: kv = KV(key="mp_mode", value=mode); db.session.add(kv)
    else: kv.value = mode
    db.session.commit()
    return ok_json({"ok": True, "mp_mode": mode})

# -------------------------------------------------------------
# Productos CRUD
# -------------------------------------------------------------
@app.get("/api/productos")
def productos_list():
    prods = Producto.query.order_by(Producto.slot_id.asc()).all()
    return jsonify([serialize_producto(p) for p in prods])

@app.post("/api/productos")
def productos_create():
    data = request.get_json(force=True)
    try:
        p = Producto(
            nombre=str(data.get("nombre", "")).strip(),
            precio=float(data.get("precio", 0)),
            cantidad=int(float(data.get("cantidad", 0))),
            slot_id=int(data.get("slot", 1)),
            porcion_litros=int(data.get("porcion_litros", 1)),
            habilitado=bool(data.get("habilitado", False)),
        )
        if p.precio < 0 or p.cantidad < 0 or p.porcion_litros < 1:
            return json_error("Valores inválidos", 400)
        if Producto.query.filter(Producto.slot_id == p.slot_id).first():
            return json_error("Slot ya asignado a otro producto", 409)
        db.session.add(p); db.session.commit()
        return ok_json({"ok": True, "producto": serialize_producto(p)}, 201)
    except Exception as e:
        db.session.rollback(); app.logger.exception("Error creando producto")
        return json_error("Error creando producto", 500, str(e))

@app.put("/api/productos/<int:pid>")
def productos_update(pid):
    data = request.get_json(force=True); p = Producto.query.get_or_404(pid)
    try:
        if "nombre" in data: p.nombre = str(data["nombre"]).strip()
        if "precio" in data: p.precio = float(data["precio"])
        if "cantidad" in data: p.cantidad = int(float(data["cantidad"]))
        if "porcion_litros" in data:
            val = int(data["porcion_litros"]); if val < 1: 
                return json_error("porcion_litros debe ser ≥ 1", 400)
            p.porcion_litros = val
        if "slot" in data:
            new_slot = int(data["slot"])
            if new_slot != p.slot_id and Producto.query.filter(Producto.slot_id == new_slot, Producto.id != p.id).first():
                return json_error("Slot ya asignado a otro producto", 409)
            p.slot_id = new_slot
        if "habilitado" in data: p.habilitado = bool(data["habilitado"])
        db.session.commit(); return ok_json({"ok": True, "producto": serialize_producto(p)})
    except Exception as e:
        db.session.rollback(); app.logger.exception("Error actualizando producto")
        return json_error("Error actualizando producto", 500, str(e))

@app.delete("/api/productos/<int:pid>")
def productos_delete(pid):
    p = Producto.query.get_or_404(pid)
    try:
        db.session.delete(p); db.session.commit()
        return ok_json({"ok": True}, 204)
    except Exception as e:
        db.session.rollback(); app.logger.exception("Error eliminando producto")
        return json_error("Error eliminando producto", 500, str(e))

@app.post("/api/productos/<int:pid>/reponer")
def productos_reponer(pid):
    p = Producto.query.get_or_404(pid)
    litros = _to_int((request.get_json(force=True) or {}).get("litros", 0))
    if litros <= 0: return json_error("Litros inválidos", 400)
    p.cantidad = max(0, int(p.cantidad or 0) + litros); db.session.commit()
    return ok_json({"ok": True, "producto": serialize_producto(p)})

@app.post("/api/productos/<int:pid>/reset_stock")
def productos_reset(pid):
    p = Producto.query.get_or_404(pid)
    litros = _to_int((request.get_json(force=True) or {}).get("litros", 0))
    if litros < 0: return json_error("Litros inválidos", 400)
    p.cantidad = int(litros); db.session.commit()
    return ok_json({"ok": True, "producto": serialize_producto(p)})

# -------------------------------------------------------------
# Pagos
# -------------------------------------------------------------
@app.get("/api/pagos")
def pagos_list():
    try:
        limit = int(request.args.get("limit", 50)); limit = max(1, min(limit, 200))
    except Exception: limit = 50
    estado = (request.args.get("estado") or "").strip()
    qsearch = (request.args.get("q") or "").strip()
    q = Pago.query
    if estado: q = q.filter(Pago.estado == estado)
    if qsearch: q = q.filter(Pago.mp_payment_id.ilike(f"%{qsearch}%"))
    pagos = q.order_by(Pago.id.desc()).limit(limit).all()
    return jsonify([{
        "id": p.id, "mp_payment_id": p.mp_payment_id, "estado": p.estado, "producto": p.producto,
        "product_id": p.product_id, "slot_id": p.slot_id, "litros": p.litros, "monto": p.monto,
        "dispensado": bool(p.dispensado),
        "created_at": p.created_at.isoformat() if p.created_at else None,
    } for p in pagos])

@app.get("/api/pagos/pendiente")
def pagos_pendiente():
    p = (Pago.query.filter(Pago.estado == "approved", Pago.dispensado == False)
         .order_by(Pago.created_at.asc()).first())
    if not p: return jsonify({"ok": True, "pago": None})
    prod = Producto.query.get(p.product_id) if p.product_id else None
    data = {"id": p.id, "mp_payment_id": p.mp_payment_id, "estado": p.estado,
            "litros": int(p.litros or 0), "slot_id": int(p.slot_id or 0),
            "product_id": int(p.product_id or 0), "producto": p.producto,
            "created_at": p.created_at.isoformat() if p.created_at else None}
    if prod:
        data["producto_nombre"] = prod.nombre
        data["porcion_litros"] = int(getattr(prod, "porcion_litros", 1))
        data["stock_litros"] = int(prod.cantidad)
    return jsonify({"ok": True, "pago": data})

@app.post("/api/pagos/<int:pid>/dispensado")
def pagos_dispensado(pid):
    p = Pago.query.get_or_404(pid)
    if p.dispensado:
        return jsonify({"ok": True, "msg": "Ya estaba confirmado", "pago": {"id": p.id, "dispensado": True}})
    prod = Producto.query.get(p.product_id)
    if not prod: return json_error("Producto no encontrado para este pago", 404)
    litros_desc = int(p.litros or 0) or 1
    prod.cantidad = max(0, int(prod.cantidad) - litros_desc)
    p.dispensado = True; p.procesado = True; db.session.commit()
    return jsonify({"ok": True, "msg": "Dispensado confirmado",
                    "pago": {"id": p.id, "dispensado": True},
                    "producto": {"id": prod.id, "stock": prod.cantidad}})

@app.post("/api/pagos/<int:pid>/fallo")
def pagos_fallo(pid):
    p = Pago.query.get_or_404(pid)
    return jsonify({"ok": True, "msg": "Registrado fallo", "pago": {"id": p.id}})

@app.post("/api/pagos/<int:pid>/reenviar")
def pagos_reenviar(pid):
    p = Pago.query.get_or_404(pid)
    if p.dispensado: return jsonify({"ok": False, "msg": "El pago ya está marcado como dispensado"})
    if p.estado != "approved": return jsonify({"ok": False, "msg": f"Estado no válido para reintento: {p.estado}"})
    if not p.slot_id or not p.litros: return jsonify({"ok": False, "msg": "Pago sin slot/litros válidos"})
    litros = int(p.litros or 1)
    ok = send_dispense_cmd(p.mp_payment_id, p.slot_id, litros, timeout_s=max(30, litros * 5))
    return jsonify({"ok": ok, "msg": "Comando reenviado" if ok else "No se pudo publicar a MQTT",
                    "pago": {"id": p.id, "mp_payment_id": p.mp_payment_id, "slot_id": p.slot_id, "litros": litros}})

# -------------------------------------------------------------
# Mercado Pago
# -------------------------------------------------------------
@app.post("/api/pagos/preferencia")
def crear_preferencia():
    data = request.get_json(force=True, silent=True) or {}
    product_id = _to_int(data.get("product_id") or 0)
    litros_req = _to_int(data.get("litros") or 0)

    prod = Producto.query.get(product_id)
    if not prod or not prod.habilitado: return json_error("producto no disponible", 400)

    litros = litros_req if litros_req > 0 else int(getattr(prod, "porcion_litros", 1) or 1)
    backend_base = BACKEND_BASE_URL or request.url_root.rstrip("/")

    mp_token, _ = get_mp_token_and_base()
    if not mp_token: return json_error("MP_ACCESS_TOKEN no configurado", 500)

    body = {
        "items": [{
            "id": str(prod.id),
            "title": prod.nombre,
            "description": prod.nombre,
            "quantity": 1,
            "currency_id": "ARS",
            "unit_price": float(prod.precio),
        }],
        "description": prod.nombre,
        "additional_info": {"items": [{"id": str(prod.id), "title": prod.nombre, "quantity": 1, "unit_price": float(prod.precio)}]},
        "metadata": {"slot_id": int(prod.slot_id), "product_id": int(prod.id), "producto": prod.nombre, "litros": int(litros)},
        "external_reference": f"pid={prod.id};slot={prod.slot_id};litros={litros}",
        "auto_return": "approved",
        "back_urls": {"success": WEB_URL, "failure": WEB_URL, "pending": WEB_URL},
        "notification_url": f"{backend_base}/api/mp/webhook",
        "statement_descriptor": "DISPEN-EASY",
    }

    mode = get_mp_mode()
    app.logger.info(f"[MP] preferencia ({mode}) req → {body}")
    try:
        r = requests.post(
            "https://api.mercadopago.com/checkout/preferences",
            headers={"Authorization": f"Bearer {mp_token}", "Content-Type": "application/json"},
            json=body, timeout=20
        )
        r.raise_for_status()
    except Exception as e:
        app.logger.exception("[MP] error al crear preferencia")
        return json_error("mp_preference_failed", 502, getattr(r, "text", str(e))[:400])

    pref = r.json() or {}
    link = pref.get("init_point") or pref.get("sandbox_init_point")
    if not link: return json_error("preferencia_sin_link", 502, pref)
    return ok_json({"ok": True, "link": link, "raw": pref, "mode": mode})

@app.post("/api/mp/webhook")
def mp_webhook():
    body = request.get_json(silent=True) or {}
    args = request.args or {}
    topic = args.get("topic") or body.get("type")
    # El propio objeto de pago indicará live_mode True/False
    mp_token, base_api = get_mp_token_and_base()

    app.logger.info(f"[MP] webhook topic={topic} args={dict(args)} body={body}")

    payment_id = None
    if topic == "payment":
        if "resource" in body and isinstance(body["resource"], str):
            try: payment_id = body["resource"].rstrip("/").split("/")[-1]
            except Exception: payment_id = None
        payment_id = payment_id or (body.get("data") or {}).get("id") or args.get("id")

    if topic == "merchant_order" and not payment_id:
        mo_id = args.get("id") or (body.get("data") or {}).get("id")
        if mo_id:
            try:
                r_mo = requests.get(f"{base_api}/merchant_orders/{mo_id}",
                                    headers={"Authorization": f"Bearer {mp_token}"}, timeout=15)
                if r_mo.ok:
                    pays = (r_mo.json() or {}).get("payments") or []
                    if pays: payment_id = str(pays[0].get("id"))
            except Exception:
                app.logger.exception("[MP] error consultando merchant_order")

    if not payment_id:
        app.logger.warning("[MP] webhook sin payment_id"); return "ok", 200

    try:
        r_pay = requests.get(f"{base_api}/v1/payments/{payment_id}",
                             headers={"Authorization": f"Bearer {mp_token}"}, timeout=15)
        r_pay.raise_for_status()
    except Exception:
        app.logger.exception(f"[MP] error HTTP payments/{payment_id}: {getattr(r_pay,'status_code',None)} {getattr(r_pay,'text','')[:400]}")
        return "ok", 200

    pay = r_pay.json() or {}
    estado = (pay.get("status") or "").lower()
    md = pay.get("metadata") or {}
    product_id = _to_int(md.get("product_id") or 0)
    slot_id = _to_int(md.get("slot_id") or 0)
    litros = _to_int(md.get("litros") or 0)

    if (not product_id or not slot_id or not litros) and pay.get("external_reference"):
        try:
            parts = dict(kv.split("=", 1) for kv in pay["external_reference"].split(";") if "=" in kv)
            product_id = product_id or _to_int(parts.get("pid") or 0)
            slot_id = slot_id or _to_int(parts.get("slot") or 0)
            litros = litros or _to_int(parts.get("litros") or 0)
        except Exception:
            app.logger.warning(f"[MP] external_reference malformado: {pay.get('external_reference')}")

    monto = int(round(float(pay.get("transaction_amount") or 0)))
    producto_txt = (md.get("producto")
        or (pay.get("additional_info", {}).get("items") or [{}])[0].get("title")
        or pay.get("description") or "")[:120]

    p = Pago.query.filter_by(mp_payment_id=str(payment_id)).first()
    if not p:
        p = Pago(mp_payment_id=str(payment_id), estado=estado or "pending", producto=producto_txt,
                 dispensado=False, procesado=False, slot_id=slot_id, litros=litros if litros > 0 else 1,
                 monto=monto, product_id=product_id, raw=pay)
        db.session.add(p)
    else:
        p.estado = estado or p.estado
        p.producto = producto_txt or p.producto
        p.slot_id = p.slot_id or slot_id
        p.product_id = p.product_id or product_id
        p.litros = p.litros or (litros if litros > 0 else p.litros)
        p.monto = p.monto or monto
        p.raw = pay

    try: db.session.commit()
    except Exception:
        db.session.rollback(); app.logger.exception("[DB] error guardando pago"); return "ok", 200

    try:
        if p.estado == "approved" and not p.dispensado and not getattr(p, "procesado", False) and p.slot_id and p.litros:
            published = send_dispense_cmd(p.mp_payment_id, p.slot_id, p.litros, timeout_s=max(30, p.litros * 5))
            if published: p.procesado = True; db.session.commit()
            else: app.logger.error(f"[MQTT] publicación falló para pago {p.mp_payment_id}")
    except Exception:
        app.logger.exception("[MQTT] no se pudo publicar orden tras approval")

    return "ok", 200

# -------------------------------------------------------------
# Orden manual (pruebas)
# -------------------------------------------------------------
@app.post("/api/dispense/orden")
def api_dispense_orden():
    data = request.get_json(force=True, silent=True) or {}
    payment_id = str(data.get("payment_id") or "").strip()
    slot_id = _to_int(data.get("slot_id") or 0)
    litros = _to_int(data.get("litros") or 0)
    if not payment_id: return json_error("Falta payment_id")
    if slot_id <= 0: return json_error("slot_id inválido")
    if litros <= 0: litros = 1
    ok = send_dispense_cmd(payment_id, slot_id, litros, timeout_s=max(30, litros*5))
    return jsonify({"ok": ok})

# -------------------------------------------------------------
# Init MQTT & run
# -------------------------------------------------------------
with app.app_context():
    try: start_mqtt_background()
    except Exception: app.logger.exception("[MQTT] error iniciando hilo")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
