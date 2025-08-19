import os
import json
import datetime as dt

from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.sql import text

import requests

# ------------------------------------------------------------------------------
# Config Flask
# ------------------------------------------------------------------------------
app = Flask(__name__)
CORS(app)

# DB (Railway ya provee DATABASE_URL)
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get("DATABASE_URL", "sqlite:///local.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

# ------------------------------------------------------------------------------
# Modelos
# ------------------------------------------------------------------------------
class Producto(db.Model):
    __tablename__ = "producto"
    id = db.Column(db.Integer, primary_key=True)
    slot_id = db.Column(db.Integer, nullable=False, default=0, index=True)
    nombre = db.Column(db.String(120), nullable=False, default="")
    precio = db.Column(db.Float, nullable=False, default=0.0)
    cantidad = db.Column(db.Integer, nullable=False, default=1)
    habilitado = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=dt.datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint("slot_id", name="uq_producto_slot"),
    )

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
    id = db.Column(db.Integer, primary_key=True)
    id_pago = db.Column(db.String(50), unique=True)     # id de MP (string)
    estado = db.Column(db.String(40))                   # pending, approved, etc
    producto = db.Column(db.String(200))                # título
    slot_id = db.Column(db.Integer)
    monto = db.Column(db.Float)
    raw = db.Column(db.Text)                            # json crudo
    created_at = db.Column(db.DateTime, nullable=False, default=dt.datetime.utcnow)


# ------------------------------------------------------------------------------
# Esquema (idempotente)
# ------------------------------------------------------------------------------
def ensure_schema():
    """
    - Crea tablas si no existen
    - En Postgres agrega columnas/índices si faltan (idempotente)
    """
    db.create_all()

    # Sólo para Postgres: ALTERs idempotentes
    dialect = db.engine.url.get_backend_name()
    if dialect.startswith("postgres"):
        stmts = [
            # producto
            "ALTER TABLE producto ADD COLUMN IF NOT EXISTS slot_id INTEGER NOT NULL DEFAULT 0;",
            "ALTER TABLE producto ADD COLUMN IF NOT EXISTS nombre VARCHAR(120) NOT NULL DEFAULT '';",
            "ALTER TABLE producto ADD COLUMN IF NOT EXISTS precio DOUBLE PRECISION NOT NULL DEFAULT 0;",
            "ALTER TABLE producto ADD COLUMN IF NOT EXISTS cantidad INTEGER NOT NULL DEFAULT 1;",
            "ALTER TABLE producto ADD COLUMN IF NOT EXISTS habilitado BOOLEAN NOT NULL DEFAULT FALSE;",
            "ALTER TABLE producto ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW();",
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname='uq_producto_slot') THEN "
            "CREATE UNIQUE INDEX uq_producto_slot ON producto(slot_id); END IF; END $$;",
            # pago
            "ALTER TABLE pago ADD COLUMN IF NOT EXISTS id_pago VARCHAR(50);",
            "ALTER TABLE pago ADD COLUMN IF NOT EXISTS estado VARCHAR(40);",
            "ALTER TABLE pago ADD COLUMN IF NOT EXISTS producto VARCHAR(200);",
            "ALTER TABLE pago ADD COLUMN IF NOT EXISTS slot_id INTEGER;",
            "ALTER TABLE pago ADD COLUMN IF NOT EXISTS monto DOUBLE PRECISION;",
            "ALTER TABLE pago ADD COLUMN IF NOT EXISTS raw TEXT;",
            "ALTER TABLE pago ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW();",
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname='pago_id_pago_key') THEN "
            "CREATE UNIQUE INDEX pago_id_pago_key ON pago(id_pago); END IF; END $$;",
        ]
        with db.engine.begin() as conn:
            for s in stmts:
                conn.execute(text(s))

# Ejecuta al levantar
with app.app_context():
    ensure_schema()
    print("[DB] esquema OK")

# ------------------------------------------------------------------------------
# Utilidades MP
# ------------------------------------------------------------------------------
def base_url() -> str:
    # request.host_url trae https://.../ con barra final
    return request.host_url

def get_mp_token() -> str:
    access = os.environ.get("MP_ACCESS_TOKEN")
    if not access:
        raise RuntimeError("MP_ACCESS_TOKEN no configurado")
    return access

def crear_preferencia_mp(prod: Producto) -> tuple[int, dict, str]:
    """
    Crea preferencia en Checkout Pro.
    Devuelve: (status_code, json_dict, raw_text)
    """
    url = "https://api.mercadopago.com/checkout/preferences"
    headers = {
        "Authorization": f"Bearer {get_mp_token()}",
        "Content-Type": "application/json",
    }

    back = base_url()  # p.ej. https://web-xxx.up.railway.app/

    payload = {
        "items": [{
            "title": prod.nombre,
            "quantity": int(prod.cantidad or 1),
            "unit_price": float(prod.precio),
            "currency_id": "ARS",
            "description": "Producto de DISPENEASY",
        }],
        "back_urls": {
            "success": back,   # podés customizar (/success) si querés
            "pending": back,
            "failure": back
        },
        "auto_return": "approved",
        "notification_url": back.rstrip("/") + "/webhook",
        "external_reference": f"prod:{prod.id}|slot:{prod.slot_id}",
        "metadata": {"producto_id": prod.id, "slot_id": prod.slot_id}
    }

    resp = requests.post(url, headers=headers, json=payload, timeout=15)

    try:
        data = resp.json()
    except Exception:
        data = {}

    # Logs para diagnóstico
    print("[MP] Creando preferencia ->", json.dumps(payload, ensure_ascii=False))
    print(f"[MP] status={resp.status_code} body={resp.text}")

    return resp.status_code, data, resp.text

def obtener_pago_mp(payment_id: str) -> dict:
    """
    Consulta /v1/payments/<id> y devuelve JSON (o {} si falla).
    """
    url = f"https://api.mercadopago.com/v1/payments/{payment_id}"
    headers = {"Authorization": f"Bearer {get_mp_token()}"}
    try:
        r = requests.get(url, headers=headers, timeout=15)
        print(f"[MP] GET payment {payment_id} -> {r.status_code}")
        return r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    except Exception as e:
        print("[MP] error consultando pago:", e)
        return {}

# ------------------------------------------------------------------------------
# Rutas base
# ------------------------------------------------------------------------------
@app.get("/")
def root():
    return "DISPENEASY backend OK"

# ------------------------------------------------------------------------------
# Productos
# ------------------------------------------------------------------------------
@app.get("/api/productos")
def listar_productos():
    prods = Producto.query.order_by(Producto.slot_id.asc()).all()
    return jsonify([p.to_dict() for p in prods])

@app.post("/api/productos/<int:slot_id>")
def upsert_producto(slot_id: int):
    data = request.get_json(silent=True) or {}
    nombre = (data.get("nombre") or "").strip()
    precio = float(data.get("precio") or 0)
    cantidad = int(data.get("cantidad") or 1)
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
def borrar_producto(slot_id: int):
    p = Producto.query.filter_by(slot_id=slot_id).first()
    if not p:
        return jsonify({"ok": True})
    db.session.delete(p)
    db.session.commit()
    return jsonify({"ok": True})

# ------------------------------------------------------------------------------
# Generar QR / Preferencia (por slot_id)
# ------------------------------------------------------------------------------
@app.get("/api/generar_qr/<int:slot_id>")
def generar_qr(slot_id: int):
    p = Producto.query.filter_by(slot_id=slot_id).first()

    if not p or not p.habilitado or not p.nombre or not p.precio or p.precio <= 0:
        return jsonify({"error": "no se pudo crear preferencia (¿producto habilitado y con nombre/precio?)"}), 400

    try:
        status, data, raw = crear_preferencia_mp(p)
    except Exception as e:
        print("[MP] excepción creando preferencia:", e)
        return jsonify({"error": "Error creando preferencia"}), 500

    if status != 201:
        # MP debe devolver 201
        return jsonify({
            "error": data.get("message") or "MP no devolvió 201",
            "detalle": data.get("cause"),
            "raw": raw
        }), 502

    link = data.get("init_point") or data.get("sandbox_init_point")
    if not link:
        return jsonify({"error": "MP no devolvió init_point", "mp": data}), 502

    return jsonify({"qr_link": link})

# ------------------------------------------------------------------------------
# Webhook Mercado Pago
# ------------------------------------------------------------------------------
@app.post("/webhook")
def webhook():
    """
    MP envía:
      - body JSON (o form) con {resource/type/data.id,...}
      - headers con firma, etc. (no la validamos en este MVP)
    Guardamos un registro idempotente por id_pago.
    """
    try:
        # Preferir JSON; si viene como form, tomar 'data.id'
        body = request.get_json(silent=True)
        if not body and request.form:
            # simulador MP suele mandar 'id', 'type', 'data[id]'
            body = {"id": request.form.get("id"),
                    "type": request.form.get("type"),
                    "data": {"id": request.form.get("data.id")}}
    except Exception:
        body = None

    print("[WEBHOOK] headers:", dict(request.headers))
    print("[WEBHOOK] body:", body)

    # Intentamos extraer payment_id
    payment_id = None
    if isinstance(body, dict):
        # distintos formatos posibles
        if "data" in body and isinstance(body["data"], dict) and body["data"].get("id"):
            payment_id = str(body["data"]["id"])
        elif "id" in body and body.get("type") == "payment":
            payment_id = str(body["id"])

    # Si no tenemos id, igualmente guardamos algo para depurar
    if not payment_id:
        rec = Pago(
            id_pago=None,
            estado="pendiente",
            producto="(via webhook)",
            slot_id=None,
            monto=None,
            raw=json.dumps({"headers": dict(request.headers), "body": body}, ensure_ascii=False)
        )
        db.session.add(rec)
        db.session.commit()
        return jsonify({"ok": True})

    # Consultamos el pago en MP
    pago_mp = obtener_pago_mp(payment_id) or {}
    status = (pago_mp.get("status") or "pendiente")
    title = None
    amount = None
    slot = None

    # Preferencia asociada (puede venir en 'additional_info' o 'description')
    try:
        if pago_mp.get("additional_info") and pago_mp["additional_info"].get("items"):
            title = pago_mp["additional_info"]["items"][0].get("title")
    except Exception:
        pass

    amount = pago_mp.get("transaction_amount") or pago_mp.get("money_release_amount")

    # Intentamos leer metadata de la preferencia (cuando MP la devuelve)
    try:
        if pago_mp.get("metadata"):
            meta = pago_mp["metadata"]
            slot = meta.get("slot_id")
            if not title and meta.get("producto_id"):
                prod = Producto.query.get(int(meta["producto_id"]))
                if prod:
                    title = prod.nombre
    except Exception:
        pass

    # Insert idempotente
    try:
        rec = Pago(
            id_pago=str(payment_id),
            estado=str(status),
            producto=str(title) if title else "Producto de DISPENEASY",
            slot_id=int(slot) if slot is not None else None,
            monto=float(amount) if amount is not None else None,
            raw=json.dumps(pago_mp, ensure_ascii=False)
        )
        db.session.add(rec)
        db.session.commit()
    except Exception as e:
        # Si ya existe, ignoramos (idempotente)
        db.session.rollback()
        print("[DB] error guardando pago:", e)

    return jsonify({"ok": True})

# ------------------------------------------------------------------------------
# Main (local)
# ------------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
