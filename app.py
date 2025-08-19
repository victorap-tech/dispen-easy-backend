import os
import io
import json
import base64
from datetime import datetime
from urllib.parse import urlparse, parse_qs

import qrcode
import requests
from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv()

# -------------------------
# Config básica
# -------------------------
app = Flask(__name__)
CORS(app)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "dispen_easy.db")
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{DB_PATH}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "").strip()
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")

# -------------------------
# Modelos
# -------------------------
class Product(db.Model):
    __tablename__ = "products"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    price = db.Column(db.Float, nullable=False, default=0.0)
    active = db.Column(db.Boolean, default=True)

class Payment(db.Model):
    __tablename__ = "payments"
    id = db.Column(db.Integer, primary_key=True)
    mp_payment_id = db.Column(db.String(64), unique=True)
    status = db.Column(db.String(50))
    amount = db.Column(db.Float, default=0.0)
    currency = db.Column(db.String(10), default="ARS")
    description = db.Column(db.String(255))
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"))
    payer_email = db.Column(db.String(200))
    is_dispensed = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    raw = db.Column(db.Text)

    product = db.relationship("Product", backref="payments")

# -------------------------
# Utils
# -------------------------
def ensure_db():
    with app.app_context():
        db.create_all()

def log(msg, *args):
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", *args, flush=True)

def png_qr_data_url(text: str) -> str:
    """Genera un PNG QR en data URL para el link de pago (init_point)."""
    qr = qrcode.QRCode(box_size=8, border=1)
    qr.add_data(text)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
    return f"data:image/png;base64,{b64}"

def fetch_mp_payment(mp_id: str):
    if not MP_ACCESS_TOKEN:
        log("MP_ACCESS_TOKEN no configurado")
        return None
    try:
        url = f"https://api.mercadopago.com/v1/payments/{mp_id}"
        headers = {"Authorization": f"Bearer {MP_ACCESS_TOKEN}"}
        r = requests.get(url, headers=headers, timeout=12)
        if r.status_code == 200:
            return r.json()
        log("Falló consulta MP", r.status_code, r.text[:300])
    except Exception as e:
        log("Excepción consultando MP:", e)
    return None

def upsert_payment_from_mp(mp_json: dict):
    if not mp_json:
        return None
    mp_id = str(mp_json.get("id") or "")
    status = mp_json.get("status")
    description = mp_json.get("description") or (mp_json.get("additional_info") or {}).get("items", [{}])[0].get("title")
    amount = (mp_json.get("transaction_amount") or 0) * 1.0
    currency = mp_json.get("currency_id") or "ARS"
    payer_email = (mp_json.get("payer") or {}).get("email")
    product_id = None
    ext_ref = mp_json.get("external_reference")
    if ext_ref:
        try:
            product_id = int(ext_ref)
        except:
            pass

    p = Payment.query.filter_by(mp_payment_id=mp_id).first()
    if not p:
        p = Payment(
            mp_payment_id=mp_id,
            status=status,
            amount=amount,
            currency=currency,
            description=description,
            product_id=product_id,
            payer_email=payer_email,
            raw=json.dumps(mp_json, ensure_ascii=False),
        )
        db.session.add(p)
    else:
        p.status = status
        p.amount = amount
        p.currency = currency
        p.description = description or p.description
        p.product_id = product_id if product_id else p.product_id
        p.payer_email = payer_email or p.payer_email
        p.raw = json.dumps(mp_json, ensure_ascii=False)
    db.session.commit()
    return p

def upsert_payment_minimal(id_str: str, status: str = None, body: dict = None):
    p = Payment.query.filter_by(mp_payment_id=id_str).first()
    if not p:
        p = Payment(
            mp_payment_id=id_str,
            status=status or "unknown",
            raw=json.dumps(body or {}, ensure_ascii=False),
        )
        db.session.add(p)
    else:
        if status:
            p.status = status
        p.raw = json.dumps(body or {}, ensure_ascii=False)
    db.session.commit()
    return p

# -------------------------
# Rutas
# -------------------------
@app.route("/")
def root():
    return "Dispen-Easy backend activo"

@app.route("/webhook", methods=["POST"])
def webhook():
    """Recibe notificaciones de MP y guarda/actualiza pagos."""
    try:
        log("Webhook headers:", dict(request.headers))
        raw_body = request.get_data(as_text=True)
        log("Webhook body:", raw_body[:1000])

        mp_id = request.args.get("id") or request.args.get("data_id") or ""
        topic = request.args.get("type") or request.args.get("topic") or ""
        body = {}
        try:
            body = request.get_json(force=False, silent=True) or {}
        except:
            body = {}

        if not mp_id:
            data = body.get("data") or {}
            mp_id = str(data.get("id") or "")
        if not mp_id and body.get("resource"):
            try:
                q = urlparse(body["resource"])
                qs = parse_qs(q.query)
                mp_id = str((qs.get("id") or [""])[0])
            except:
                pass
        if not topic:
            topic = body.get("type") or body.get("action") or ""

        log(f"Parsed: topic={topic}, mp_id={mp_id}")

        if not mp_id:
            upsert_payment_minimal("sin_id", status="unknown", body=body)
            return jsonify({"ok": True, "note": "sin id"}), 200

        mp_json = fetch_mp_payment(mp_id)
        if mp_json:
            p = upsert_payment_from_mp(mp_json)
            log(f"Pago upsert MP: {p.mp_payment_id} status={p.status}")
            return jsonify({"ok": True, "mp_id": mp_id, "status": p.status}), 200

        p = upsert_payment_minimal(mp_id, status=(body.get("status") or None), body=body)
        log(f"Pago upsert minimal: {p.mp_payment_id} status={p.status}")
        return jsonify({"ok": True, "mp_id": mp_id, "status": p.status}), 200

    except Exception as e:
        log("Error /webhook:", e)
        return jsonify({"ok": False, "error": str(e)}), 500

# ---- Pagos
@app.route("/api/payments", methods=["GET"])
def list_payments():
    status = request.args.get("status")
    dispensed = request.args.get("dispensed")
    q = Payment.query
    if status:
        q = q.filter_by(status=status)
    if dispensed is not None:
        q = q.filter_by(is_dispensed=(dispensed.lower() == "true"))
    q = q.order_by(Payment.created_at.desc())
    return jsonify([{
        "id": p.id,
        "mp_payment_id": p.mp_payment_id,
        "status": p.status,
        "amount": p.amount,
        "currency": p.currency,
        "description": p.description,
        "product_id": p.product_id,
        "payer_email": p.payer_email,
        "is_dispensed": p.is_dispensed,
        "created_at": p.created_at.isoformat(),
    } for p in q.all()])

@app.route("/api/payments/pending", methods=["GET"])
def list_pending():
    q = Payment.query.filter_by(status="approved", is_dispensed=False).order_by(Payment.created_at.desc())
    return jsonify([{
        "id": p.id,
        "mp_payment_id": p.mp_payment_id,
        "status": p.status,
        "amount": p.amount,
        "currency": p.currency,
        "description": p.description,
        "product_id": p.product_id,
        "payer_email": p.payer_email,
        "is_dispensed": p.is_dispensed,
        "created_at": p.created_at.isoformat(),
    } for p in q.all()])

@app.route("/api/mark_dispensed", methods=["POST"])
def mark_dispensed():
    data = request.get_json(force=True)
    mp_id = str(data.get("mp_payment_id") or "")
    if not mp_id:
        return jsonify({"ok": False, "error": "mp_payment_id requerido"}), 400
    p = Payment.query.filter_by(mp_payment_id=mp_id).first()
    if not p:
        return jsonify({"ok": False, "error": "Pago no encontrado"}), 404
    p.is_dispensed = True
    db.session.commit()
    return jsonify({"ok": True, "mp_payment_id": mp_id})

# ---- Productos
@app.route("/api/products", methods=["GET"])
def products_list():
    prods = Product.query.order_by(Product.id.asc()).all()
    return jsonify([{"id": x.id, "name": x.name, "price": x.price, "active": x.active} for x in prods])

@app.route("/api/products", methods=["POST"])
def products_create():
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    price = float(data.get("price") or 0)
    if not name:
        return jsonify({"ok": False, "error": "name requerido"}), 400
    pr = Product(name=name, price=price, active=True)
    db.session.add(pr)
    db.session.commit()
    return jsonify({"ok": True, "id": pr.id})

@app.route("/api/products/<int:pid>", methods=["DELETE"])
def products_delete(pid):
    pr = Product.query.get(pid)
    if not pr:
        return jsonify({"ok": False, "error": "no encontrado"}), 404
    db.session.delete(pr)
    db.session.commit()
    return jsonify({"ok": True})

# ---- Generar Preferencia + QR (funcional)
@app.route("/api/generar_qr/<int:product_id>", methods=["POST"])
def generar_qr(product_id):
    """
    Crea una preferencia de MP (Checkout Pro) para el producto dado,
    con external_reference=product_id y notification_url al /webhook.
    Devuelve init_point + QR (data URL PNG) para pegar en la salida.
    """
    if not MP_ACCESS_TOKEN:
        return jsonify({"ok": False, "error": "Falta MP_ACCESS_TOKEN en .env"}), 500
    if not PUBLIC_URL:
        return jsonify({"ok": False, "error": "Falta PUBLIC_URL en .env"}), 500

    pr = Product.query.get(product_id)
    if not pr or not pr.active:
        return jsonify({"ok": False, "error": "Producto no encontrado o inactivo"}), 404

    body = {
        "items": [{
            "title": pr.name,
            "quantity": 1,
            "unit_price": round(float(pr.price), 2),
            "currency_id": "ARS",
        }],
        "external_reference": str(pr.id),
        "notification_url": f"{PUBLIC_URL}/webhook",
        # Opcional: back_urls, auto_return, payer, etc.
        "back_urls": {
            "success": f"{PUBLIC_URL}/ok",
            "failure": f"{PUBLIC_URL}/fail",
            "pending": f"{PUBLIC_URL}/pending",
        },
        "auto_return": "approved"
    }
    headers = {"Authorization": f"Bearer {MP_ACCESS_TOKEN}", "Content-Type": "application/json"}
    r = requests.post("https://api.mercadopago.com/checkout/preferences", headers=headers, json=body, timeout=15)
    if r.status_code not in (200, 201):
        return jsonify({"ok": False, "error": f"MP error {r.status_code}", "detail": r.text}), 500

    pref = r.json()
    init_point = pref.get("init_point") or pref.get("sandbox_init_point")
    if not init_point:
        return jsonify({"ok": False, "error": "No se obtuvo init_point de MP"}), 500

    qr_data_url = png_qr_data_url(init_point)
    return jsonify({
        "ok": True,
        "product": {"id": pr.id, "name": pr.name, "price": pr.price},
        "preference_id": pref.get("id"),
        "init_point": init_point,
        "qr_png_data_url": qr_data_url
    })

# -------------------------
# Main
# -------------------------
if __name__ == "__main__":
    ensure_db()
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
