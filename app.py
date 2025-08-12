from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
import requests, qrcode, io, base64, os
import traceback

app = Flask(__name__)

CORS(
    app,
    resources={r"/api/*": {"origins": [
        "https://dispen-easy-web-production.up.railway.app",
        "http://localhost:3000"
    ]}},
    methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)

# DB
# ------------------------
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL")
db = SQLAlchemy(app)

# ---- Helper: consultar Merchant Order ----
def mp_get_merchant_order(mo_id: str, token: str):
    """
    Devuelve (info_dict, http_status, raw) de la merchant_order de MP.
    """
    url = f"https://api.mercadolibre.com/merchant_orders/{mo_id}"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        r = requests.get(url, headers=headers, timeout=12)
    except Exception as e:
        return None, 599, str(e)
    try:
        raw_json = r.json()
    except Exception:
        raw_json = r.text
    if r.status_code != 200:
        return None, r.status_code, raw_json
    return raw_json, r.status_code, raw_json


# Si ya tenés este helper, dejalo; si no, dejá este:
def mp_get_payment(payment_id: str, token: str):
    url = f"https://api.mercadopago.com/v1/payments/{payment_id}"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        r = requests.get(url, headers=headers, timeout=12)
    except Exception as e:
        return None, 599, str(e)
    try:
        raw_json = r.json()
    except Exception:
        raw_json = r.text
    if r.status_code != 200:
        return None, r.status_code, raw_json
    data = r.json()
    info = {
        "id_pago": str(data.get("id")),
        "estado": data.get("status"),  # approved/pending/rejected/in_process
        "producto": (data.get("description")
                     or (data.get("additional_info", {}) or {}).get("items", [{}])[0].get("title"))
    }
    return info, 200, raw_json

# ------------------------
# Modelos
# ------------------------
class Pago(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    id_pago = db.Column(db.String(120), unique=True, nullable=False)
    estado = db.Column(db.String(80), nullable=False)
    producto = db.Column(db.String(120), nullable=True)
    dispensado = db.Column(db.Boolean, default=False)

class Producto(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(100), nullable=False)
    precio = db.Column(db.Float, nullable=False)
    cantidad = db.Column(db.Integer, nullable=False)  # stock / cantidad


# Crear tablas si no existen
with app.app_context():
    db.create_all()
    print("Tablas creadas")

# ------------------------
# Preflight (OPTIONS) para CORS
# ------------------------
@app.route("/api/productos", methods=["OPTIONS"])
def productos_options():
    return "", 204

@app.route("/api/productos/<int:id>", methods=["OPTIONS"])
def productos_id_options(id):
    return "", 204

# ------------------------
# CRUD Productos
# ------------------------
@app.route("/api/productos", methods=["GET"])
def listar_productos():
    productos = Producto.query.all()
    data = [{"id": p.id, "nombre": p.nombre, "precio": p.precio, "cantidad": p.cantidad} for p in productos]
    return jsonify(data)

@app.route("/api/productos", methods=["POST"])
def agregar_producto():
    data = request.json or {}
    try:
        nuevo = Producto(
            nombre=data["nombre"],
            precio=float(data["precio"]),
            cantidad=int(data["cantidad"])
        )
        db.session.add(nuevo)
        db.session.commit()
        return jsonify({"mensaje": "Producto agregado correctamente"})
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": f"No se pudo agregar el producto: {e}"}), 400

@app.route("/api/productos/<int:id>", methods=["DELETE"])
def eliminar_producto(id):
    p = Producto.query.get(id)
    if not p:
        return jsonify({"error": "Producto no encontrado"}), 404
    db.session.delete(p)
    db.session.commit()
    return jsonify({"mensaje": "Producto eliminado"})

# ------------------------
# Generar QR de pago (Mercado Pago)
# ------------------------
@app.route("/api/generar_qr/<int:id>", methods=["GET"])
def generar_qr(id):
    producto = Producto.query.get(id)
    if not producto:
        return jsonify({"error": "Producto no encontrado"}), 404

    token = os.getenv("MP_ACCESS_TOKEN")
    if not token:
        return jsonify({"error": "Falta MP_ACCESS_TOKEN en variables de entorno"}), 500

    url = "https://api.mercadopago.com/checkout/preferences"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
   payload = {
    "items": [{
        "title": producto.nombre,
        "quantity": 1,
        "unit_price": float(producto.precio)
    }],
    "description": producto.nombre,  # <-- para que todos los flujos vean el nombre
    "additional_info": {
        "items": [{"title": producto.nombre}]
    },
    "metadata": {
        "producto_id": producto.id,
        "producto_nombre": producto.nombre
    },
    "external_reference": f"prod:{producto.id}",
    "notification_url": "https://web-production-e7d2.up.railway.app/webhook",
    "back_urls": {
        "success": "https://dispen-easy-web-production.up.railway.app/",
        "pending": "https://dispen-easy-web-production.up.railway.app/",
        "failure": "https://dispen-easy-web-production.up.railway.app/"
    },
    "auto_return": "approved"
}
    

    resp = requests.post(url, headers=headers, json=payload)
    if resp.status_code != 201:
        # Loguea para ver exactamente qué dice MP
        try:
            detalle = resp.json()
        except Exception:
            detalle = resp.text
        print("MP error:", resp.status_code, detalle)
        return jsonify({"error": "No se pudo generar link de pago", "detalle": detalle}), 502

    link = resp.json().get("init_point")
    import qrcode, io, base64
    img = qrcode.make(link)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    qr_base64 = base64.b64encode(buf.getvalue()).decode("utf-8")
    return jsonify({"qr_base64": qr_base64, "link": link})
   

# ---- WEBHOOK Mercado Pago ----
@app.route("/webhook", methods=["POST"])
def webhook():
    body = request.get_json(silent=True) or {}
    headers = dict(request.headers)
    print("▶︎ Webhook headers =", headers)
    print("▶︎ Webhook raw body =", body)

    token = os.getenv("MP_ACCESS_TOKEN")
    if not token:
        print("[webhook] Falta MP_ACCESS_TOKEN")
        return "", 200  # respondemos 200 para que MP no reintente infinito

    # MP puede mandar varios formatos:
    topic = body.get("topic") or body.get("type") or body.get("action")  # p.ej: "merchant_order" o "payment.created"
    resource = body.get("resource")  # p.ej: "https://api.mercadolibre.com/merchant_orders/33120295977"
    data_id = (body.get("data") or {}).get("id")  # cuando es payment suele venir acá

    # --- Caso 1: Merchant Order ---
    if resource and "merchant_orders" in resource:
        mo_id = resource.rstrip("/").split("/")[-1]
        info_mo, st, raw = mp_get_merchant_order(mo_id, token)
        if not info_mo:
            print("[webhook] Error merchant_order", st, raw)
            return "", 200

        # Extraemos pagos dentro de la orden
        payments = info_mo.get("payments", []) or []
        approved = any(p.get("status") == "approved" for p in payments)
        payment_id = str(payments[0].get("id")) if payments else f"MO:{mo_id}"

        # Título del item (si vino)
        titulo = ""
        items = info_mo.get("items") or []
        if items:
            titulo = items[0].get("title") or ""

        # Upsert en tu tabla Pago
        pago = Pago.query.filter_by(id_pago=payment_id).first()
        if not pago:
            pago = Pago(id_pago=payment_id, estado="pendiente", producto=titulo, dispensado=False)
            db.session.add(pago)

        pago.estado = "aprobado" if approved else "pendiente"
        if titulo:
            pago.producto = titulo

        db.session.commit()
        print(f"[webhook] MerchantOrder procesada. payment_id={payment_id} estado={pago.estado}")
        return "", 200

    # --- Caso 2: Payment (fallback por si MP envía 'payment.*') ---
    if data_id:
        info_pay, st, raw = mp_get_payment(str(data_id), token)
        if not info_pay:
            print("[webhook] Error payment", st, raw)
            return "", 200

        pago = Pago.query.filter_by(id_pago=info_pay["id_pago"]).first()
        if not pago:
            pago = Pago(
                id_pago=info_pay["id_pago"],
                estado=info_pay.get("estado") or "pendiente",
                producto=info_pay.get("producto") or "",
                dispensado=False
            )
            db.session.add(pago)
        else:
            pago.estado = info_pay.get("estado") or pago.estado
            if info_pay.get("producto"):
                pago.producto = info_pay["producto"]

        db.session.commit()
        print(f"[webhook] Payment procesado. id={pago.id_pago} estado={pago.estado}")
        return "", 200

    # Si no sabemos qué es, igual devolvemos 200
    print("[webhook] Evento no reconocido, se ignora.")
    return "", 200

# ==== ENDPOINT SIMPLE PARA AUDITAR PAGOS ====
@app.route("/api/pagos", methods=["GET"])
def listar_pagos():
    pagos = Pago.query.order_by(Pago.id.desc()).all()
    out = []
    for p in pagos:
        out.append({
            "id": p.id,
            "id_pago": p.id_pago,
            "estado": p.estado,
            "producto": p.producto,
            "dispensado": p.dispensado
        })
    return jsonify(out)
# ------------------------
# Consultar pago pendiente
# ------------------------
@app.route("/check_payment_pendiente", methods=["GET"])
def check_pendiente():
    pago = Pago.query.filter_by(estado="pendiente", dispensado=False).first()
    if pago:
        return jsonify({"id_pago": pago.id_pago})
    return jsonify({"mensaje": "No hay pagos pendientes"}), 204

# ------------------------
# Marcar como dispensado
# ------------------------
@app.route("/marcar_dispensado", methods=["POST"])
def marcar_dispensado():
    data = request.json or {}
    id_pago = data.get("id_pago")
    pago = Pago.query.filter_by(id_pago=id_pago).first()
    if pago:
        pago.estado = "aprobado"
        pago.dispensado = True
        db.session.commit()
        return jsonify({"mensaje": "Pago marcado como dispensado"})
    return jsonify({"error": "Pago no encontrado"}), 404

@app.route("/", methods=["GET"])
def home():
    return "✅ Backend funcionando"
# ------------------------
# Entrada local (Gunicorn maneja producción)
# ------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
