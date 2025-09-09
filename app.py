# app.py
import os
import logging
import threading
import requests
import json as _json

from flask import Flask, jsonify, request, make_response
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy import UniqueConstraint, text as sqltext
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

UMBRAL_ALERTA_LTS = int(os.getenv("UMBRAL_ALERTA_LTS", "3") or 3)
STOCK_RESERVA_LTS = int(os.getenv("STOCK_RESERVA_LTS", "1") or 1)

# ---------------- App/DB ----------------
app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL or "sqlite:///local.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

CORS(app, resources={r"/api/*": {"origins": "*"}}, allow_headers=["Content-Type", "x-admin-secret"])
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
    __tablename__ = "producto"
    id = db.Column(db.Integer, primary_key=True)
    dispenser_id = db.Column(db.Integer, db.ForeignKey("dispenser.id", ondelete="SET NULL"), nullable=True, index=True)
    nombre = db.Column(db.String(100), nullable=False)
    precio = db.Column(db.Float, nullable=False)     # precio base por litro
    cantidad = db.Column(db.Integer, nullable=False) # stock en litros
    slot_id = db.Column(db.Integer, nullable=False)  # 1..6
    porcion_litros = db.Column(db.Integer, nullable=False, server_default="1")
    bundle_precios = db.Column(JSONB, nullable=True) # ej: {"2": 1800, "3": 2800}
    habilitado = db.Column(db.Boolean, nullable=False, server_default=db.text("false"))

    __table_args__ = (UniqueConstraint("dispenser_id", "slot_id", name="uq_disp_slot"),)

class Pago(db.Model):
    __tablename__ = "pago"
    id = db.Column(db.Integer, primary_key=True)
    mp_payment_id = db.Column(db.String(120), nullable=False, unique=True, index=True)
    estado = db.Column(db.String(80), nullable=False)
    producto = db.Column(db.String(120), nullable=False, default="")
    dispensado = db.Column(db.Boolean, nullable=False, server_default=db.text("false"))
    procesado = db.Column(db.Boolean, nullable=False, server_default=db.text("false"))
    slot_id = db.Column(db.Integer, nullable=False, default=0)
    litros = db.Column(db.Integer, nullable=False, default=1)
    monto = db.Column(db.Integer, nullable=False, default=0)
    product_id = db.Column(db.Integer, nullable=False, default=0)
    dispenser_id = db.Column(db.Integer, nullable=False, default=0)
    device_id = db.Column(db.String(80), nullable=True, default="")
    raw = db.Column(JSONB, nullable=True)

with app.app_context():
    db.create_all()
    if not KV.query.get("mp_mode"):
        db.session.add(KV(key="mp_mode", value="test"))
        db.session.commit()
    try:
        db.session.execute(sqltext("ALTER TABLE producto ADD COLUMN IF NOT EXISTS bundle_precios JSONB"))
        db.session.commit()
    except Exception:
        db.session.rollback()

# ---------------- Helpers ----------------
def ok_json(data, status=200): return jsonify(data), status
def json_error(msg, status=400): return jsonify({"error": msg}), status
def _to_int(x, default=0):
    try: return int(x)
    except Exception: return default

def serialize_producto(p: Producto) -> dict:
    return {
        "id": p.id, "dispenser_id": p.dispenser_id, "nombre": p.nombre,
        "precio": float(p.precio), "cantidad": int(p.cantidad), "slot": int(p.slot_id),
        "porcion_litros": int(p.porcion_litros), "bundle_precios": p.bundle_precios or {},
        "habilitado": bool(p.habilitado),
    }

def compute_total_price_ars(prod: Producto, litros: int) -> int:
    litros = int(litros or 1)
    bundles = prod.bundle_precios or {}
    if str(litros) in bundles:
        return int(float(bundles[str(litros)]))
    return int(round(float(prod.precio) * litros))

def _html_raw(html: str):
    r = make_response(html, 200)
    r.headers["Content-Type"] = "text/html; charset=utf-8"
    return r

# ---------------- UI seleccionar litros ----------------
@app.get("/ui/seleccionar")
def ui_seleccionar():
    pid = _to_int(request.args.get("pid") or 0)
    if not pid:
        return _html_raw("<h1>Error</h1><p>Falta parámetro <code>pid</code></p>")
    prod = Producto.query.get(pid)
    if not prod or not prod.habilitado:
        return _html_raw("<h1>No disponible</h1><p>Producto sin stock o deshabilitado</p>")
    disp = Dispenser.query.get(prod.dispenser_id) if prod.dispenser_id else None
    if not disp or not disp.activo:
        return _html_raw("<h1>No disponible</h1><p>Dispenser no disponible</p>")

    backend = BACKEND_BASE_URL or request.url_root.rstrip("/")
    tmpl = """<!doctype html><html lang="es"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Seleccionar litros</title>
<style>
body{margin:0;background:#0b1220;color:#e5e7eb;font-family:Inter,system-ui}
.box{max-width:720px;margin:12vh auto;background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.08);border-radius:16px;padding:20px}
.opt{flex:1;min-width:170px;background:#111827;border:1px solid #374151;border-radius:12px;padding:14px;text-align:center;cursor:pointer}
.opt[aria-disabled="true"]{opacity:.5;cursor:not-allowed}
.L{font-size:28px;font-weight:800}
.price{margin-top:6px;font-size:18px;font-weight:700;color:#10b981}
</style></head><body>
<div class="box">
  <h1>__NOMBRE__</h1>
  <div>Dispenser <code>__DEVICE__</code> · Slot <b>__SLOT__</b></div>
  <div id="row" style="display:flex;gap:12px;flex-wrap:wrap;margin-top:12px"></div>
  <div id="msg"></div>
</div>
<script>
const fmt = n => new Intl.NumberFormat('es-AR',{style:'currency',currency:'ARS'}).format(n);
async function load(){
  const res = await fetch('__BACKEND__/api/productos/__PID__/opciones');
  const js = await res.json();
  const row = document.getElementById('row');
  const msg = document.getElementById('msg');
  row.innerHTML='';
  if(!js.ok){ msg.innerHTML='<span style="color:red">No disponible</span>'; return; }
  js.opciones.forEach(o=>{
    const d=document.createElement('div');
    d.className='opt';
    if(!o.disponible) d.setAttribute('aria-disabled','true');
    d.innerHTML = `<div class="L">${o.litros} L</div><div class="price">${o.precio_final?fmt(o.precio_final):'—'}</div>`;
    d.onclick=async()=>{
      if(!o.disponible) return;
      try{
        const r=await fetch('__BACKEND__/api/pagos/preferencia',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({product_id:__PID__,litros:o.litros})});
        const jr=await r.json();
        if(jr.ok && jr.link) window.location.href=jr.link;
        else alert(jr.error||'No se pudo crear el pago');
      }catch(e){alert('Error de red');}
    };
    row.appendChild(d);
  });
}
load();
</script></body></html>"""

    html = (tmpl
        .replace("__BACKEND__", backend)
        .replace("__PID__", str(pid))
        .replace("__NOMBRE__", prod.nombre)
        .replace("__DEVICE__", disp.device_id or "")
        .replace("__SLOT__", str(prod.slot_id))
    )
    return _html_raw(html)

# ---------------- Health ----------------
@app.get("/")
def health(): return ok_json({"status": "ok"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
