# app.py – Dispenser agua fría/caliente
import os
import logging
import threading
import requests
import json as _json
import time
from collections import defaultdict
from queue import Queue
from threading import Lock

from flask import Flask, jsonify, request, make_response, Response
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from sqlalchemy import text as sqltext
import paho.mqtt.client as mqtt

# ---------------- Config ----------------
DATABASE_URL = os.getenv("DATABASE_URL", "")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

BACKEND_BASE_URL = (os.getenv("BACKEND_BASE_URL", "") or "").rstrip("/")
WEB_URL = os.getenv("WEB_URL", "https://example.com").strip().rstrip("/")

MP_ACCESS_TOKEN_TEST = os.getenv("MP_ACCESS_TOKEN_TEST", "").strip()
MP_ACCESS_TOKEN_LIVE = os.getenv("MP_ACCESS_TOKEN_LIVE", "").strip()

MQTT_HOST = os.getenv("MQTT_HOST", "").strip()
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883") or 1883)
MQTT_USER = os.getenv("MQTT_USER", "")
MQTT_PASS = os.getenv("MQTT_PASS", "")

ADMIN_SECRET = os.getenv("ADMIN_SECRET", "").strip()

# ---------------- App/DB ----------------
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

# ---------------- Modelos ----------------
class KV(db.Model):
    __tablename__ = "kv"
    key = db.Column(db.String(50), primary_key=True)
    value = db.Column(db.String(200), nullable=False)

class Dispenser(db.Model):
    __tablename__ = "dispenser"
    id = db.Column(db.Integer, primary_key=True)
    device_id = db.Column(db.String(80), nullable=False, unique=True, index=True)
    nombre = db.Column(db.String(100), nullable=True, default="")
    activo = db.Column(db.Boolean, nullable=False, server_default=db.text("true"))
    created_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now(), nullable=False)

class Producto(db.Model):
    """
    Producto simple para agua:
    - nombre
    - precio (por uso)
    - slot_id (1 = frío, 2 = caliente, por ejemplo)
    - habilitado
    """
    __tablename__ = "producto"
    id = db.Column(db.Integer, primary_key=True)
    dispenser_id = db.Column(db.Integer, db.ForeignKey("dispenser.id", ondelete="SET NULL"), nullable=True, index=True)
    nombre = db.Column(db.String(100), nullable=False)
    precio = db.Column(db.Float, nullable=False)          # $ por uso
    slot_id = db.Column(db.Integer, nullable=False)       # 1..N
    habilitado = db.Column(db.Boolean, nullable=False, server_default=db.text("false"))
    created_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now(), nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), onupdate=db.func.now())

class Pago(db.Model):
    __tablename__ = "pago"
    id = db.Column(db.Integer, primary_key=True)
    mp_payment_id = db.Column(db.String(120), nullable=False, unique=True, index=True)
    estado = db.Column(db.String(80), nullable=False)
    producto = db.Column(db.String(120), nullable=False, default="")
    dispensado = db.Column(db.Boolean, nullable=False, server_default=db.text("false"))
    procesado = db.Column(db.Boolean, nullable=False, server_default=db.text("false"))
    slot_id = db.Column(db.Integer, nullable=False, default=0)
    litros = db.Column(db.Integer, nullable=False, default=1)  # fijo 1 (no se usa, pero lo dejamos)
    monto = db.Column(db.Integer, nullable=False, default=0)
    product_id = db.Column(db.Integer, nullable=False, default=0)
    dispenser_id = db.Column(db.Integer, nullable=False, default=0)
    device_id = db.Column(db.String(80), nullable=True, default="")
    raw = db.Column(db.JSON, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now(), nullable=False)

with app.app_context():
    db.create_all()
    # modo MP por defecto
    if not KV.query.get("mp_mode"):
        db.session.add(KV(key="mp_mode", value="test"))
        db.session.commit()
    # 1 dispenser por defecto
    if Dispenser.query.count() == 0:
        db.session.add(Dispenser(device_id="dispen-01", nombre="dispen-01 (por defecto)", activo=True))
        db.session.commit()

# ---------------- Helpers ----------------
def ok_json(data, status=200):
    return jsonify(data), status

def json_error(msg, status=400, extra=None):
    p = {"error": msg}
    if extra is not None:
        p["detail"] = extra
    return jsonify(p), status

def _to_int(x, default=0):
    try:
        return int(x)
    except Exception:
        try:
            return int(float(x))
        except Exception:
            return default

def serialize_producto(p: Producto) -> dict:
    return {
        "id": p.id,
        "dispenser_id": p.dispenser_id,
        "nombre": p.nombre,
        "precio": float(p.precio),
        "slot": int(p.slot_id),
        "habilitado": bool(p.habilitado),
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }

def serialize_dispenser(d: Dispenser) -> dict:
    return {
        "id": d.id,
        "device_id": d.device_id,
        "nombre": d.nombre or "",
        "activo": bool(d.activo),
        "created_at": d.created_at.isoformat() if d.created_at else None,
    }

# ---------------- Auth guard ----------------
PUBLIC_PATHS = {
    "/", "/gracias",
    "/api/mp/webhook", "/webhook", "/mp/webhook",
    "/api/pagos/preferencia",
    "/api/config",
    "/ui/seleccionar",
}

@app.before_request
def _auth_guard():
    if request.method == "OPTIONS":
        return "", 200
    p = request.path
    # público
    if p in PUBLIC_PATHS:
        return None
    # sin contraseña configurada → todo abierto
    if not ADMIN_SECRET:
        return None
    # con contraseña → header obligatorio
    if request.headers.get("x-admin-secret") != ADMIN_SECRET:
        return json_error("unauthorized", 401)
    return None

# ---------------- MP tokens ----------------
def get_mp_mode() -> str:
    row = KV.query.get("mp_mode")
    return (row.value if row else "test").lower()

def get_mp_token_and_base() -> tuple[str, str]:
    mode = get_mp_mode()
    if mode == "live":
        return MP_ACCESS_TOKEN_LIVE, "https://api.mercadopago.com"
    return MP_ACCESS_TOKEN_TEST, "https://api.sandbox.mercadopago.com"

# ---------------- MQTT ----------------
_mqtt_client = None
_mqtt_lock = threading.Lock()

def topic_cmd(device_id: str) -> str:
    return f"dispen/{device_id}/cmd/dispense"

def topic_status_wild() -> str:
    return "dispen/+/status"

def topic_event_wild() -> str:
    return "dispen/+/event"

# ---- SSE infra ----
_sse_clients = []
_sse_lock = Lock()

def _sse_broadcast(data: dict):
    with _sse_lock:
        dead = []
        for q in _sse_clients:
            try:
                q.put_nowait(data)
            except Exception:
                dead.append(q)
        for q in dead:
            try:
                _sse_clients.remove(q)
            except Exception:
                pass

@app.get("/api/events/stream")
def sse_stream():
    if ADMIN_SECRET and (request.args.get("secret") or "") != ADMIN_SECRET:
        return json_error("unauthorized", 401)
    q = Queue(maxsize=100)
    with _sse_lock:
        _sse_clients.append(q)

    def gen():
        yield "retry: 5000\n\n"
        try:
            while True:
                data = q.get()
                yield f"data: {_json.dumps(data, ensure_ascii=False)}\n\n"
        except GeneratorExit:
            with _sse_lock:
                try:
                    _sse_clients.remove(q)
                except Exception:
                    pass

    return Response(gen(), mimetype="text/event-stream")

# ---- Estado online/offline simple ----
last_status = defaultdict(lambda: {"status": "unknown", "t": 0})

def _mqtt_on_connect(client, userdata, flags, rc, props=None):
    app.logger.info(f"[MQTT] conectado rc={rc}")
    client.subscribe(topic_status_wild(), qos=1)
    client.subscribe(topic_event_wild(), qos=1)

def _mqtt_on_message(client, userdata, msg):
    try:
        raw = msg.payload.decode("utf-8", "ignore")
    except Exception:
        raw = "<binario>"
    app.logger.info(f"[MQTT] RX topic={msg.topic} payload={raw}")

    # Evento botón físico → SSE
    if msg.topic.startswith("dispen/") and msg.topic.endswith("/event"):
        try:
            data = _json.loads(raw or "{}")
        except Exception:
            data = {}
        if str(data.get("event") or "") == "button_press":
            try:
                dev = msg.topic.split("/")[1]
            except Exception:
                dev = ""
            slot = int(data.get("slot") or 0)
            _sse_broadcast({"type": "button_press", "device_id": dev, "slot": slot})
        return

    # Estado ONLINE/OFFLINE → SSE
    if msg.topic.startswith("dispen/") and msg.topic.endswith("/status"):
        try:
            data = _json.loads(raw or "{}")
        except Exception:
            return
        dev = str(data.get("device") or "").strip()
        st = str(data.get("status") or "").lower().strip()
        now = time.time()
        last_status[dev] = {"status": st, "t": now}
        _sse_broadcast({"type": "device_status", "device_id": dev, "status": st})
        return

def start_mqtt_background():
    if not MQTT_HOST:
        app.logger.warning("[MQTT] MQTT_HOST no configurado; no se inicia MQTT")
        return

    def _run():
        global _mqtt_client
        _mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="dispen-backend")
        if MQTT_USER or MQTT_PASS:
            _mqtt_client.username_pw_set(MQTT_USER, MQTT_PASS)
        if int(MQTT_PORT) == 8883:
            try:
                _mqtt_client.tls_set()
            except Exception as e:
                app.logger.error(f"[MQTT] TLS: {e}")
        _mqtt_client.on_connect = _mqtt_on_connect
        _mqtt_client.on_message = _mqtt_on_message
        _mqtt_client.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
        _mqtt_client.loop_forever()

    threading.Thread(target=_run, name="mqtt-thread", daemon=True).start()

def send_dispense_cmd(device_id: str, payment_id: str, slot_id: int, litros: int = 1, timeout_s: int = 30) -> bool:
    if not MQTT_HOST:
        return False
    msg = {
        "payment_id": str(payment_id),
        "slot_id": int(slot_id),
        "litros": int(litros or 1),  # el ESP puede ignorar o usar como “nivel”
        "timeout_s": int(timeout_s or 30),
    }
    payload = _json.dumps(msg, ensure_ascii=False)
    with _mqtt_lock:
        if not _mqtt_client:
            return False
        t = topic_cmd(device_id)
        info = _mqtt_client.publish(t, payload, qos=1, retain=False)
        return info.rc == mqtt.MQTT_ERR_SUCCESS

# ---------------- Health ----------------
@app.get("/")
def health():
    return ok_json({"status": "ok"})

# ---------------- Config ----------------
@app.get("/api/config")
def api_get_config():
    return ok_json({"mp_mode": get_mp_mode()})

@app.post("/api/mp/mode")
def api_set_mode():
    data = request.get_json(force=True, silent=True) or {}
    mode = str(data.get("mode") or "").lower()
    if mode not in ("test", "live"):
        return json_error("modo inválido (test|live)", 400)
    kv = KV.query.get("mp_mode") or KV(key="mp_mode", value=mode)
    kv.value = mode
    db.session.merge(kv)
    db.session.commit()
    return ok_json({"ok": True, "mp_mode": mode})

# ---------------- Dispensers ----------------
@app.get("/api/dispensers")
def dispensers_list():
    ds = Dispenser.query.order_by(Dispenser.id.asc()).all()
    return jsonify([serialize_dispenser(d) for d in ds])

@app.put("/api/dispensers/<int:did>")
def dispensers_update(did):
    d = Dispenser.query.get_or_404(did)
    data = request.get_json(force=True, silent=True) or {}
    if "nombre" in data:
        d.nombre = str(data["nombre"]).strip()
    if "activo" in data:
        d.activo = bool(data["activo"])
    if "device_id" in data:
        nid = str(data["device_id"]).strip()
        if nid and nid != d.device_id:
            if Dispenser.query.filter(Dispenser.device_id == nid, Dispenser.id != d.id).first():
                return json_error("device_id ya usado", 409)
            d.device_id = nid
    db.session.commit()
    return ok_json({"ok": True, "dispenser": serialize_dispenser(d)})

# ---------------- Productos CRUD ----------------
@app.get("/api/productos")
def productos_list():
    disp_id = _to_int(request.args.get("dispenser_id") or 0)
    q = Producto.query
    if disp_id:
        q = q.filter(Producto.dispenser_id == disp_id)
    prods = q.order_by(Producto.dispenser_id.asc(), Producto.slot_id.asc()).all()
    return jsonify([serialize_producto(p) for p in prods])

@app.post("/api/productos")
def productos_create():
    data = request.get_json(force=True, silent=True) or {}
    try:
        disp_id = _to_int(data.get("dispenser_id") or 0)
        if not disp_id or not Dispenser.query.get(disp_id):
            return json_error("dispenser_id inválido", 400)
        nombre = str(data.get("nombre", "")).strip()
        precio = float(data.get("precio", 0))
        slot_id = int(data.get("slot", 1))
        if not nombre:
            return json_error("nombre requerido", 400)
        if precio <= 0:
            return json_error("precio debe ser > 0", 400)

        p = Producto(
            dispenser_id=disp_id,
            nombre=nombre,
            precio=precio,
            slot_id=slot_id,
            habilitado=bool(data.get("habilitado", True)),
        )
        db.session.add(p)
        db.session.commit()
        return ok_json({"ok": True, "producto": serialize_producto(p)}, 201)
    except Exception as e:
        db.session.rollback()
        app.logger.exception("Error creando producto")
        return json_error("Error creando producto", 500, str(e))

@app.put("/api/productos/<int:pid>")
def productos_update(pid):
    data = request.get_json(force=True, silent=True) or {}
    p = Producto.query.get_or_404(pid)
    try:
        if "dispenser_id" in data:
            new_d = _to_int(data["dispenser_id"])
            if new_d and new_d != p.dispenser_id and Dispenser.query.get(new_d):
                p.dispenser_id = new_d
        if "nombre" in data:
            p.nombre = str(data["nombre"]).strip()
        if "precio" in data:
            val = float(data["precio"])
            if val <= 0:
                return json_error("precio debe ser > 0", 400)
            p.precio = val
        if "slot" in data:
            p.slot_id = int(data["slot"])
        if "habilitado" in data:
            p.habilitado = bool(data["habilitado"])

        db.session.commit()
        return ok_json({"ok": True, "producto": serialize_producto(p)})
    except Exception as e:
        db.session.rollback()
        return json_error("Error actualizando producto", 500, str(e))

# ---------------- Pagos ----------------
@app.get("/api/pagos")
def pagos_list():
    try:
        limit = int(request.args.get("limit", 50))
        limit = max(1, min(limit, 200))
    except Exception:
        limit = 50
    estado = (request.args.get("estado") or "").strip()
    qsearch = (request.args.get("q") or "").strip()
    q = Pago.query
    if estado:
        q = q.filter(Pago.estado == estado)
    if qsearch:
        q = q.filter(Pago.mp_payment_id.ilike(f"%{qsearch}%"))
    pagos = q.order_by(Pago.id.desc()).limit(limit).all()
    return jsonify([
        {
            "id": p.id,
            "mp_payment_id": p.mp_payment_id,
            "estado": p.estado,
            "producto": p.producto,
            "product_id": p.product_id,
            "dispenser_id": p.dispenser_id,
            "device_id": p.device_id,
            "slot_id": p.slot_id,
            "litros": p.litros,
            "monto": p.monto,
            "dispensado": bool(p.dispensado),
            "created_at": p.created_at.isoformat() if p.created_at else None,
        }
        for p in pagos
    ])

# -------------- preferencia (precio fijo por producto) --------------
@app.post("/api/pagos/preferencia")
def crear_preferencia_api():
    data = request.get_json(force=True, silent=True) or {}
    product_id = _to_int(data.get("product_id") or 0)

    token, _base_api = get_mp_token_and_base()
    if not token:
        return json_error("MP token no configurado", 500)

    prod = Producto.query.get(product_id)
    if not prod or not prod.habilitado:
        return json_error("producto no disponible", 400)
    disp = Dispenser.query.get(prod.dispenser_id) if prod.dispenser_id else None
    if not disp or not disp.activo:
        return json_error("dispenser no disponible", 400)

    monto_final = int(round(float(prod.precio)))

    backend_base = BACKEND_BASE_URL or request.url_root.rstrip("/")
    external_ref = f"pid={prod.id};slot={prod.slot_id};disp={disp.id};dev={disp.device_id}"
    body = {
        "items": [{
            "id": str(prod.id),
            "title": prod.nombre,
            "description": prod.nombre,
            "quantity": 1,
            "currency_id": "ARS",
            "unit_price": float(monto_final),
        }],
        "description": prod.nombre,
        "metadata": {
            "slot_id": int(prod.slot_id),
            "product_id": int(prod.id),
            "producto": prod.nombre,
            "litros": 1,
            "dispenser_id": int(disp.id),
            "device_id": disp.device_id,
            "precio_final": int(monto_final),
        },
        "external_reference": external_ref,
        "auto_return": "approved",
        "back_urls": {
            "success": f"{WEB_URL}/gracias?status=success",
            "failure": f"{WEB_URL}/gracias?status=failure",
            "pending": f"{WEB_URL}/gracias?status=pending",
        },
        "notification_url": f"{backend_base}/api/mp/webhook",
        "statement_descriptor": "DISPEN-EASY",
    }

    try:
        r = requests.post(
            "https://api.mercadopago.com/checkout/preferences",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=body,
            timeout=20,
        )
        r.raise_for_status()
    except Exception as e:
        detail = getattr(r, "text", str(e))[:600]
        return json_error("mp_preference_failed", 502, detail)

    pref = r.json() or {}
    link = pref.get("init_point") or pref.get("sandbox_init_point")
    if not link:
        return json_error("preferencia_sin_link", 502, pref)
    return ok_json({"ok": True, "link": link, "raw": pref, "precio_final": monto_final})

# ---------------- Página que se usa en el QR ----------------
@app.get("/ui/seleccionar")
def ui_seleccionar():
    """
    Antes mostraba opciones de litros.
    Ahora:
      - recibe pid
      - crea la preferencia
      - redirige automáticamente a MercadoPago.
    """
    pid = _to_int(request.args.get("pid") or 0)
    if not pid:
        return _html("Producto no encontrado", "<p>Falta parámetro <code>pid</code>.</p>")
    prod = Producto.query.get(pid)
    if not prod or not prod.habilitado:
        return _html("No disponible", "<p>Producto no disponible.</p>")
    disp = Dispenser.query.get(prod.dispenser_id) if prod.dispenser_id else None
    if not disp or not disp.activo:
        return _html("No disponible", "<p>Dispenser no disponible.</p>")

    backend = BACKEND_BASE_URL or request.url_root.rstrip("/")
    tmpl = f"""
<!doctype html>
<html lang="es"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Pagar {prod.nombre}</title>
<style>
  body{{margin:0;background:#0b1220;color:#e5e7eb;font-family:Inter,system-ui,Segoe UI,Roboto}}
  .box{{max-width:720px;margin:12vh auto;background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.08);border-radius:16px;padding:20px;text-align:center}}
  .btn{{margin-top:16px;padding:10px 16px;border-radius:999px;border:none;background:#10b981;color:#05251c;font-weight:700;cursor:pointer}}
  .err{{color:#fca5a5}}
</style>
</head><body>
<div class="box">
  <h1>{prod.nombre}</h1>
  <p>Dispenser <code>{disp.device_id if disp else ""}</code> · Slot <b>{prod.slot_id}</b></p>
  <p>Generando link de pago…</p>
  <p id="msg"></p>
  <button class="btn" id="btn" style="display:none">Ir al pago</button>
</div>
<script>
  async function go(){{
    const msg = document.getElementById('msg');
    const btn = document.getElementById('btn');
    try{{
      const r = await fetch('{backend}/api/pagos/preferencia', {{
        method:'POST', headers:{{'Content-Type':'application/json'}},
        body: JSON.stringify({{ product_id: {pid} }})
      }});
      const jr = await r.json();
      if(jr.ok && jr.link) {{
        window.location.href = jr.link;
      }} else {{
        msg.innerHTML = '<span class="err">'+(jr.error || 'No se pudo crear el pago')+'</span>';
        btn.style.display='inline-block';
        btn.onclick = ()=> window.location.reload();
      }}
    }}catch(e){{
      msg.innerHTML = '<span class="err">Error de red</span>';
      btn.style.display='inline-block';
      btn.onclick = ()=> window.location.reload();
    }}
  }}
  go();
</script>
</body></html>
"""
    return _html_raw(tmpl)

def _html(title: str, body_html: str):
    html = f"""<!doctype html><html lang="es"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{title}</title>
</head><body style="background:#0b1220;color:#e5e7eb;font-family:Inter,system-ui,Segoe UI,Roboto">
<div style="max-width:720px;margin:14vh auto;background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.08);border-radius:16px;padding:20px">
<h1 style="margin:0 0 8px">{title}</h1>
{body_html}
</div></body></html>"""
    r = make_response(html, 200)
    r.headers["Content-Type"] = "text/html; charset=utf-8"
    return r

def _html_raw(html: str):
    r = make_response(html, 200)
    r.headers["Content-Type"] = "text/html; charset=utf-8"
    return r

# ---------------- Webhook MP ----------------
@app.post("/api/mp/webhook")
def mp_webhook():
    body = request.get_json(silent=True) or {}
    args = request.args or {}
    topic = args.get("topic") or body.get("type")
    live_mode = bool(body.get("live_mode", True))
    base_api = "https://api.mercadopago.com" if live_mode else "https://api.sandbox.mercadopago.com"
    token, _ = get_mp_token_and_base()

    payment_id = None
    if topic == "payment":
        if "resource" in body and isinstance(body["resource"], str):
            try:
                payment_id = body["resource"].rstrip("/").split("/")[-1]
            except Exception:
                payment_id = None
        payment_id = payment_id or (body.get("data") or {}).get("id") or args.get("id")

    if not payment_id:
        return "ok", 200

    try:
        r_pay = requests.get(
            f"{base_api}/v1/payments/{payment_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        r_pay.raise_for_status()
    except Exception:
        return "ok", 200

    pay = r_pay.json() or {}
    estado = (pay.get("status") or "").lower()
    md = pay.get("metadata") or {}
    product_id = _to_int(md.get("product_id") or 0)
    slot_id = _to_int(md.get("slot_id") or 0)
    litros = _to_int(md.get("litros") or 0) or 1
    dispenser_id = _to_int(md.get("dispenser_id") or 0)
    device_id = str(md.get("device_id") or "")
    monto = _to_int(md.get("precio_final") or 0) or _to_int(pay.get("transaction_amount") or 0)

    producto_txt = (md.get("producto") or pay.get("description") or "")[:120]
    p = Pago.query.filter_by(mp_payment_id=str(payment_id)).first()
    if not p:
        p = Pago(
            mp_payment_id=str(payment_id),
            estado=estado or "pending",
            producto=producto_txt,
            dispensado=False,
            procesado=False,
            slot_id=slot_id,
            litros=litros,
            monto=monto,
            product_id=product_id,
            dispenser_id=dispenser_id,
            device_id=device_id,
            raw=pay,
        )
        db.session.add(p)
    else:
        p.estado = estado or p.estado
        p.producto = producto_txt or p.producto
        p.slot_id = p.slot_id or slot_id
        p.product_id = p.product_id or product_id
        p.litros = p.litros or litros
        p.monto = p.monto or monto
        p.dispenser_id = p.dispenser_id or dispenser_id
        p.device_id = p.device_id or device_id
        p.raw = pay

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return "ok", 200

    # Enviar comando al ESP cuando el pago está aprobado
    try:
        if p.estado == "approved" and not p.dispensado and not getattr(p, "procesado", False) and p.slot_id:
            dev = p.device_id
            if not dev and p.product_id:
                pr = Producto.query.get(p.product_id)
                if pr and pr.dispenser_id:
                    d = Dispenser.query.get(pr.dispenser_id)
                    dev = d.device_id if d else ""
            if dev:
                published = send_dispense_cmd(dev, p.mp_payment_id, p.slot_id, litros=1, timeout_s=30)
                if published:
                    p.procesado = True
                    db.session.commit()
    except Exception:
        pass

    return "ok", 200

@app.post("/webhook")
def mp_webhook_alias1():
    return mp_webhook()

@app.post("/mp/webhook")
def mp_webhook_alias2():
    return mp_webhook()

# ---------------- Estado para Admin (fallback) ----------------
@app.get("/api/dispensers/status")
def api_disp_status():
    out = []
    for dev, info in last_status.items():
        out.append({"device_id": dev, "status": info["status"]})
    return jsonify(out)

# ---------------- Gracias ----------------
@app.get("/gracias")
def pagina_gracias():
    status = (request.args.get("status") or "").lower()
    if status in ("success", "approved"):
        title = "¡Gracias por su compra!"
        subtitle = '<span class="ok">Pago aprobado.</span> Presione el botón para dispensar.'
    elif status in ("pending", "in_process"):
        title = "Pago pendiente"
        subtitle = "Tu pago está en revisión. Si se aprueba, se dispensará automáticamente."
    else:
        title = "Pago no completado"
        subtitle = '<span class="err">El pago fue cancelado o rechazado.</span>'
    return _html(title, f"<p>{subtitle}</p>")

# ---------------- Inicializar MQTT ----------------
with app.app_context():
    try:
        start_mqtt_background()
    except Exception:
        app.logger.exception("[MQTT] error iniciando hilo")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
