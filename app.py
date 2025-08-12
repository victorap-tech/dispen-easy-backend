from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
import requests, qrcode, io, base64, os

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

# --- Helper para consultar pago por payment_id ---
def mp_get_payment(payment_id: str, token: str):
    """
    Devuelve un diccionario con datos clave del pago.
    """
    url = f"https://api.mercadopago.com/v1/payments/{payment_id}"
    headers = {"Authorization": f"Bearer {token}"}

    try:
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code != 200:
            return None, r.status_code, r.text

        data = r.json()
        info = {
            "id_pago": str(data.get("id")),
            "estado": data.get("status"),  # approved / pending / rejected / in_process
            "producto": (data.get("description")
                        or (data.get("additional_info") or {}).get("items", [{}])[0].get("title")),
            "monto": data.get("transaction_amount"),
            "moneda": data.get("currency_id"),
            "payer_email": (data.get("payer") or {}).get("email")
        }
        return info, 200, None
    except Exception as e:
        return None, 500, str(e)

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
        "items": [
            {"title": producto.nombre, "quantity": 1, "unit_price": float(producto.precio)}
        ],
        "notification_url": "https://web-production-e7d2.up.railway.app/webhook",  # ajustá tu dominio
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
   

# =========================
# Webhook de Mercado Pago
# =========================
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json()
    print("Webhook recibido:", data)

    if data and 'type' in data and data['type'] == 'payment':
        payment_id = str(data['data']['id'])

        # Consultar datos del pago en MercadoPago
        mp_token = os.getenv("MP_ACCESS_TOKEN")
        info, status, raw = mp_get_payment(payment_id, mp_token)

        if status == 200 and info:
            nuevo_pago = Pago(
                id_pago=info["id_pago"],
                estado=info["estado"],
                producto=info["producto"],
                monto=info["monto"],
                moneda=info["moneda"],
                payer_email=info["payer_email"],
                dispensado=False
            )
            db.session.add(nuevo_pago)
            db.session.commit()

            return jsonify({"status": "ok"}), 200
        else:
            print("MP GET payment error:", raw)
            return jsonify({"error": "payment not found"}), 404

    return jsonify({"status": "ignored"}), 200


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
