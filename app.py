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

#----Helper para consultar Mercadopago----
def mp_get_payment(payment_id: str, token: str):
    """
    Devuelve un dict con campos clave del pago en MP.
    """
    url = f"https://api.mercadopago.com/v1/payments/{payment_id}"
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.get(url, headers=headers, timeout=10)
    if r.status_code != 200:
        # devolvemos info mínima para log
        try:
            return None, r.status_code, r.json()
        except Exception:
            return None, r.status_code, r.text

    data = r.json()
    info = {
        "id_pago": str(data.get("id")),
        "estado": data.get("status"),                 # approved / pending / rejected / in_process
        "producto": (data.get("description")          # si seteaste description
                     or (data.get("additional_info") or {}).get("items", [{}])[0].get("title")
                     or ""),
        "monto": data.get("transaction_amount"),
        "moneda": data.get("currency_id"),
        "payer_email": (data.get("payer") or {}).get("email"),
    }
    return info, 200, None
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
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        token = os.getenv("MP_ACCESS_TOKEN")
        if not token:
            # Si falta el token, devolvemos 500 para que quede logueado y lo veas
            return jsonify({"error": "Falta MP_ACCESS_TOKEN en variables de entorno"}), 500

        body = request.get_json(silent=True) or {}
        print("⚡ Webhook recibido:", body)

        # --- 1) Detectar el payment_id de distintas formas ---
        payment_id = None

        # a) Formato nuevo {"action": "...", "data": {"id": "123"}}
        payment_id = (body.get("data") or {}).get("id")

        # b) Formato "resource" con URL (merchant_orders/... o payments/...)
        if not payment_id and "resource" in body:
            try:
                resource = body["resource"]
                # Tomamos el último segmento de la URL (el ID)
                payment_id = resource.rstrip("/").split("/")[-1]
            except Exception:
                pass

        # c) A veces llega por querystring ?id=123
        if not payment_id:
            payment_id = request.args.get("id")

        if not payment_id:
            # No sabemos qué pago es; devolvemos 202 para que MP no reintente infinito
            print("Webhook sin payment_id identificable")
            return "", 202

        # --- 2) Consultar el pago real en MP con el helper ---
        info, status, raw = mp_get_payment(payment_id, token)
        if status != 200 or info is None:
            print("MP GET payment error:", status, raw)
            # 202 = acepto el webhook, pero no proceso (evita reintentos agresivos)
            return "", 202

        # info trae: id_pago, estado, producto, monto, moneda, payer_email (según tu helper)
        print("MP payment info:", info)

        # --- 3) Guardar / actualizar en la DB ---
        pago = Pago.query.filter_by(id_pago=info["id_pago"]).first()
        if not pago:
            pago = Pago(
                id_pago=info["id_pago"],
                estado=info.get("estado", "pendiente"),
                producto=info.get("producto") or "",
                dispensado=False,
            )
            db.session.add(pago)
        else:
            # Actualizamos por si cambió el estado (pending -> approved, etc.)
            pago.estado = info.get("estado", pago.estado)
            if info.get("producto"):
                pago.producto = info["producto"]

        db.session.commit()

        # Respondemos OK a MP
        return "", 200

    except Exception as e:
        # Log para depurar sin romper el webhook
        print("Webhook exception:", repr(e))
        # 202 para que MP no entre en loop de reintentos si hay un bug temporal
        return "", 202

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

# ------------------------
# Entrada local (Gunicorn maneja producción)
# ------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
