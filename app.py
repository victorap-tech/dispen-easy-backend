# app.py
import os, json, re, datetime as dt
import requests
from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.dialects.postgresql import JSONB
import logging
from flask_cors import CORS
# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "").strip()   # usa el de PROD
MP_WEBHOOK_URL = os.getenv("MP_WEBHOOK_URL", "").strip()   # opcional (info)

app = Flask(__name__)
CORS(app)
app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL or "sqlite:///local.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

logging.basicConfig(level=logging.INFO)
app.logger.setLevel(logging.INFO)

# -----------------------------------------------------------------------------
# Modelos
# -----------------------------------------------------------------------------
class Producto(db.Model):
    __tablename__ = "producto"
    id         = db.Column(db.Integer, primary_key=True)
    nombre     = db.Column(db.String(100), nullable=False)
    precio     = db.Column(db.Float, nullable=False)
    cantidad   = db.Column(db.Float, nullable=False)         # litros
    slot_id    = db.Column(db.Integer, nullable=False, unique=True, index=True)
    created_at = db.Column(db.DateTime(timezone=True), default=dt.datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), default=dt.datetime.utcnow, onupdate=dt.datetime.utcnow, nullable=False)
    habilitado = db.Column(db.Boolean, default=False, nullable=False)

    def to_dict(self):
        return dict(
            id=self.id, nombre=self.nombre, precio=self.precio,
            cantidad=self.cantidad, slot=self.slot_id, habilitado=self.habilitado
        )

class Pago(db.Model):
    __tablename__ = "pago"
    id            = db.Column(db.Integer, primary_key=True)
    mp_payment_id = db.Column(db.String(120), nullable=False, unique=True, index=True)
    estado        = db.Column(db.String(80),  nullable=False)     # e.g. approved, rejected, pending
    producto      = db.Column(db.String(120), nullable=True)      # nombre del producto (conveniencia)
    dispensado    = db.Column(db.Boolean, default=False, nullable=False)
    slot_id       = db.Column(db.Integer, nullable=True)
    monto         = db.Column(db.Integer, nullable=True)          # entero en ARS (ej 10)
    raw           = db.Column(JSONB, nullable=True)
    created_at    = db.Column(db.DateTime(timezone=True), default=dt.datetime.utcnow, nullable=False)
    product_id    = db.Column(db.Integer, nullable=True)

    def to_dict(self):
        return dict(
            id=self.id, mp_payment_id=self.mp_payment_id, estado=self.estado,
            producto=self.producto, dispensado=self.dispensado, slot_id=self.slot_id,
            monto=self.monto, created_at=self.created_at.isoformat(), product_id=self.product_id
        )

with app.app_context():
    db.create_all()

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def mp_headers():
    if not ACCESS_TOKEN:
        app.logger.warning("[MP] Falta MP_ACCESS_TOKEN")
    return {
        "Authorization": f"Bearer {ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }

def parse_id_from_url(url: str):
    """extrae el último número grande de una URL (por si viene en body.resource)"""
    if not url:
        return None
    m = re.search(r"(\d{6,})$", url.strip("/"))
    return m.group(1) if m else None

# -----------------------------------------------------------------------------
# Rutas básicas
# -----------------------------------------------------------------------------
@app.get("/")
def root():
    return jsonify({"status": "ok", "message": "Backend Dispen-Easy operativo"})

# ---------------- Productos CRUD ----------------
@app.get("/api/productos")
def productos_list():
    prods = Producto.query.order_by(Producto.id.asc()).all()
    return jsonify([p.to_dict() for p in prods])

@app.post("/api/productos")
def productos_create():
    data = request.get_json(force=True) or {}
    p = Producto(
        nombre=str(data.get("nombre","")).strip(),
        precio=float(data.get("precio",0)),
        cantidad=float(data.get("cantidad",0)),
        slot_id=int(data.get("slot",1)),
        habilitado=bool(data.get("habilitado", False)),
    )
    db.session.add(p)
    db.session.commit()
    return jsonify({"ok": True, "producto": p.to_dict()}), 201

@app.put("/api/productos/<int:pid>")
def productos_update(pid):
    p = Producto.query.get_or_404(pid)
    data = request.get_json(force=True) or {}
    if "nombre" in data:     p.nombre = str(data["nombre"]).strip()
    if "precio" in data:     p.precio = float(data["precio"])
    if "cantidad" in data:   p.cantidad = float(data["cantidad"])
    if "slot" in data:       p.slot_id = int(data["slot"])
    if "habilitado" in data: p.habilitado = bool(data["habilitado"])
    db.session.commit()
    return jsonify({"ok": True, "producto": p.to_dict()})

@app.delete("/api/productos/<int:pid>")
def productos_delete(pid):
    p = Producto.query.get_or_404(pid)
    db.session.delete(p)
    db.session.commit()
    return "", 204

@app.post("/api/productos/<int:pid>/reponer")
def productos_reponer(pid):
    p = Producto.query.get_or_404(pid)
    litros = float((request.get_json(force=True) or {}).get("litros", 0))
    if litros > 0:
        p.cantidad = max(0.0, p.cantidad + litros)
        db.session.commit()
    return jsonify({"ok": True, "producto": p.to_dict()})

@app.post("/api/productos/<int:pid>/reset_stock")
def productos_reset(pid):
    p = Producto.query.get_or_404(pid)
    litros = float((request.get_json(force=True) or {}).get("litros", 0))
    if litros >= 0:
        p.cantidad = litros
        db.session.commit()
    return jsonify({"ok": True, "producto": p.to_dict()})

# ---------------- Pago: crear preferencia ----------------
@app.post("/api/pagos/preferencia")
def pagos_preferencia():
    data = request.get_json(force=True) or {}
    product_id = int(data.get("product_id") or data.get("producto_id") or 0)
    slot_id    = int(data.get("slot_id") or data.get("slot") or 0)

    prod = Producto.query.get_or_404(product_id)

    payload = {
        "items": [{
            "title": prod.nombre,
            "quantity": 1,
            "unit_price": round(float(prod.precio), 2),
            "currency_id": "ARS"
        }],
        # ¡IMPORTANTE!: metadata para que el webhook sepa qué producto/slot era
        "metadata": {
            "product_id": prod.id,
            "slot_id": slot_id or prod.slot_id
        },
        # Opcional: URLs de retorno (no imprescindibles para QR)
        "auto_return": "approved",
        # Si configuraste el webhook en el panel, no hace falta pasarlo acá,
        # pero si querés forzarlo:
        # "notification_url": MP_WEBHOOK_URL or "<tu_url>/api/mp/webhook",
    }

    r = requests.post(
        "https://api.mercadopago.com/checkout/preferences",
        headers=mp_headers(),
        data=json.dumps(payload),
        timeout=15
    )
    r.raise_for_status()
    pref = r.json()
    # si estás en modo prueba, viene sandbox_init_point; en prod, init_point
    link = pref.get("init_point") or pref.get("sandbox_init_point")
    return jsonify({"ok": True, "preference_id": pref.get("id"), "init_point": link})

# -----------------------------------------------------------------------------
# Webhook MP (maneja payment y merchant_order)
# -----------------------------------------------------------------------------
@app.post("/api/mp/webhook")
def mp_webhook():
    # Mercado Pago manda datos en querystring y/o body
    args = request.args or {}
    body = request.get_json(silent=True) or {}

    topic = args.get("topic") or body.get("topic") or body.get("type")
    # live_mode True => producción, False => sandbox
    live_mode = bool(body.get("live_mode", True))
    base_api = "https://api.mercadopago.com" if live_mode else "https://api.sandbox.mercadopago.com"

    app.logger.info(f"[MP] webhook topic={topic} live_mode={live_mode} args={dict(args)}")
    if body:
        app.logger.info(f"[MP] raw body={body}")

    payment_id = None
    merchant_order_id = None

    # Si viene merchant_order, primero obtener payments
    if topic == "merchant_order":
        merchant_order_id = args.get("id") or parse_id_from_url(body.get("resource",""))
        app.logger.info(f"[MP] webhook topic=merchant_order live_mode={live_mode}")
        if not merchant_order_id:
            app.logger.warning("[MP] merchant_order sin id")
            return "ok", 200
        r_mo = requests.get(
            f"{base_api}/merchant_orders/{merchant_order_id}",
            headers=mp_headers(),
            timeout=15
        )
        r_mo.raise_for_status()
        mo = r_mo.json()
        pays = mo.get("payments") or []
        if not pays:
            app.logger.warning(f"[MP] merchant_order {merchant_order_id} sin payments")
            return "ok", 200
        payment_id = str(pays[0].get("id"))

    # Si viene payment directo
    if topic == "payment":
        payment_id = args.get("id") or str((body.get("data") or {}).get("id") or "")

    # A veces Mercado Pago no envía topic pero sí "resource" con la URL
    if not payment_id and not merchant_order_id and body.get("resource"):
        # puede ser /collections/ o /payments/
        rid = parse_id_from_url(body["resource"])
        if "payments" in body["resource"]:
            payment_id = rid
        elif "merchant_orders" in body["resource"]:
            merchant_order_id = rid
            r_mo = requests.get(
                f"{base_api}/merchant_orders/{merchant_order_id}",
                headers=mp_headers(),
                timeout=15
            )
            r_mo.raise_for_status()
            mo = r_mo.json()
            pays = mo.get("payments") or []
            if pays:
                payment_id = str(pays[0].get("id"))

    if not payment_id:
        app.logger.warning("[MP] No se encontró payment_id en la notificación")
        return "ok", 200

    # Traer el pago real
    try:
        r_pay = requests.get(
            f"{base_api}/v1/payments/{payment_id}",
            headers=mp_headers(),
            timeout=15
        )
        r_pay.raise_for_status()
        pay = r_pay.json()
    except requests.HTTPError as e:
        app.logger.error(f"[MP] HTTPError: {e} for url: {e.request.url if hasattr(e,'request') else ''}")
        return "ok", 200

    status = pay.get("status") or ""
    amount = int(round(float(pay.get("transaction_amount") or 0)))
    metadata = pay.get("metadata") or {}
    prod_id = metadata.get("product_id")
    slot_id = metadata.get("slot_id")

    # fallback: intentar leer de additional_info
    if not prod_id:
        addi = pay.get("additional_info") or {}
        items = addi.get("items") or []
        if items:
            # si en tu preferencia pusiste items[0].id como product_id podrías usarlo
            maybe = items[0].get("id")
            try:
                prod_id = int(maybe)
            except (TypeError, ValueError):
                prod_id = None

    prod_name = None
    if prod_id:
        prod = Producto.query.get(prod_id)
        if prod:
            prod_name = prod.nombre
            # (opcional) ejemplo de descuento de stock: 1 litro por pago
            # prod.cantidad = max(0.0, float(prod.cantidad) - 1.0)

    # Upsert de Pago
    existing = Pago.query.filter_by(mp_payment_id=str(payment_id)).first()
    if existing:
        existing.estado     = status
        existing.monto      = amount
        existing.producto   = prod_name or existing.producto
        existing.product_id = prod_id if prod_id else existing.product_id
        if slot_id is not None:
            existing.slot_id = slot_id
        existing.raw = pay
        db.session.commit()
        app.logger.info(f"[MP] pago {payment_id} actualizado -> {status}")
    else:
        nuevo = Pago(
            mp_payment_id=str(payment_id),
            estado=status,
            producto=prod_name,
            product_id=prod_id,
            slot_id=slot_id,
            monto=amount,
            raw=pay,
            dispensado=False,
        )
        db.session.add(nuevo)
        db.session.commit()
        app.logger.info(f"[MP] pago {payment_id} registrado -> {status}")

    return "ok", 200

# -----------------------------------------------------------------------------
# (Opcional) ruta simple para ping del webhook sin auth
# -----------------------------------------------------------------------------
@app.post("/webhook")
def mp_webhook_alias():
    # por si en MP dejaste /webhook, redirijo a /api/mp/webhook
    return mp_webhook()

# -----------------------------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
