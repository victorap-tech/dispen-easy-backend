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

    # Buscar el nombre del producto en distintos lugares
    producto_nombre = (
        data.get("description")
        or (data.get("additional_info", {}).get("items", [{}])[0].get("title"))
        or (data.get("metadata", {}).get("producto_nombre"))
        or "Producto sin nombre"
    )

    info = {
        "id_pago": str(data.get("id")),
        "estado": data.get("status"),  # approved / pending / rejected / in_process
        "producto": producto_nombre
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

# --------------------------------------------
# Generar QR de pago (Mercado Pago)
# --------------------------------------------
@app.route("/api/generar_qr/<int:id>", methods=["GET"])
def generar_qr(id):
    # 1) Buscar producto
    producto = Producto.query.get(id)
    if not producto:
        return jsonify({"error": "Producto no encontrado"}), 404

    # 2) Token MP desde variables de entorno
    token = os.getenv("MP_ACCESS_TOKEN")
    if not token:
        return jsonify({"error": "Falta MP_ACCESS_TOKEN en variables de entorno"}), 500

    # 3) Crear preferencia en Mercado Pago
    url = "https://api.mercadopago.com/checkout/preferences"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }

    # Ajusta estos dominios si cambiaron
    FRONTEND_URL = "https://dispen-easy-web-production.up.railway.app/"
    BACKEND_URL  = "https://web-production-e7d2.up.railway.app"

    payload = {
        "items": [
            {
                "title": producto.nombre,                 # Nombre visible
                "quantity": 1,
                "unit_price": float(producto.precio),     # Importe
            }
        ],
        # Suma redundancia del nombre para que aparezca en todos los flujos
        "description": producto.nombre,
        "additional_info": {
            "items": [{"title": producto.nombre}]
        },
        # Metadata útil para rastrear en tu BD
        "metadata": {
            "producto_id": producto.id,
            "producto_nombre": producto.nombre,
        },
        # Para reconciliar desde el webhook
        "external_reference": f"prod:{producto.id}",
        # Tu webhook (backend)
        "notification_url": f"{BACKEND_URL}/webhook",
        # A dónde vuelve el payer si abre en navegador
        "back_urls": {
            "success": FRONTEND_URL,
            "pending": FRONTEND_URL,
            "failure": FRONTEND_URL,
        },
        "auto_return": "approved",
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=15)
    except Exception as e:
        return jsonify({"error": "No se pudo contactar a MP", "detalle": str(e)}), 502

    if resp.status_code != 201:
        # Log por si necesitás ver detallado en Railway
        try:
            detalle = resp.json()
        except Exception:
            detalle = resp.text
        print("MP error:", resp.status_code, detalle)
        return jsonify({"error": "No se pudo crear preferencia", "detalle": detalle}), 502

    # 4) Obtener link de pago
    data = resp.json()
    link = data.get("init_point") or data.get("sandbox_init_point")
    if not link:
        return jsonify({"error": "Preferencia creada sin link"}), 502

    # 5) Generar QR PNG en base64 a partir del link
    img = qrcode.make(link)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    qr_base64 = base64.b64encode(buf.getvalue()).decode("utf-8")

    # 6) Devolver QR + link (por si querés abrirlo en nueva pestaña)
    return jsonify({
        "qr_base64": qr_base64,
        "link": link,
        "preferencia_id": data.get("id"),
        "titulo": producto.nombre,
        "precio": float(producto.precio),
    })

# ---------- Webhook Mercado Pago ----------
@app.route("/webhook", methods=["POST"])
def webhook():
    # log crudo
    raw = request.get_data(as_text=True) or ""
    print("Webhook raw body =", raw)

    token = os.getenv("MP_ACCESS_TOKEN")
    if not token:
        return jsonify({"error": "MP_ACCESS_TOKEN faltante"}), 500

    data = request.get_json(silent=True) or {}

    # Caso A: llega 'resource' con una merchant_order
    resource = data.get("resource")
    if isinstance(resource, str) and "/merchant_orders/" in resource:
        mo_id = resource.rsplit("/", 1)[-1]
        sc, mo = mp_get_merchant_order(mo_id, token)
        print("[webhook] MerchantOrder resp:", sc, mo)

        # Tomamos el primer pago si existe
        payments = (mo or {}).get("payments") or []
        if payments:
            payment_id = str(payments[0].get("id"))
            sc, pay = mp_get_payment(payment_id, token)
            print("[webhook] Payment resp:", sc, pay)
            if sc == 200 and isinstance(pay, dict):
                estado = str(pay.get("status", "pending"))
                # Nombre de producto: priorizamos description, luego metadata, luego items
                prod = (
                    pay.get("description")
                    or (pay.get("additional_info") or {}).get("items", [{}])[0].get("title")
                    or (pay.get("metadata") or {}).get("producto_nombre")
                    or "producto"
                )
                _upsert_pago(payment_id, estado, prod)
                return "", 200

        # Si no hay pagos en la MO, igual respondemos 200 para evitar reintentos infinitos
        return "", 200

    # Caso B: evento directo de pago (payment.created / payment.updated)
    action = data.get("action", "")
    if action.startswith("payment."):
        payment_id = str((data.get("data") or {}).get("id") or data.get("id") or "")
        if payment_id:
            sc, pay = mp_get_payment(payment_id, token)
            print("[webhook] Payment resp:", sc, pay)
            if sc == 200 and isinstance(pay, dict):
                estado = str(pay.get("status", "pending"))
                prod = (
                    pay.get("description")
                    or (pay.get("additional_info") or {}).get("items", [{}])[0].get("title")
                    or (pay.get("metadata") or {}).get("producto_nombre")
                    or "producto"
                )
                _upsert_pago(payment_id, estado, prod)
        return "", 200

    # Si no matchea ninguno, OK igual
    print("[webhook] evento no reconocido:", data)
    return "", 200


def _upsert_pago(id_pago: str, estado: str, producto: str):
    """Crea o actualiza el registro en la tabla pagos."""
    try:
        pago = Pago.query.filter_by(id_pago=id_pago).first()
        if pago:
            pago.estado = estado
            if producto:
                pago.producto = producto
        else:
            pago = Pago(id_pago=id_pago, estado=estado, producto=producto, dispensado=False)
            db.session.add(pago)
        db.session.commit()
        print(f"[webhook] Pago guardado/actualizado id={id_pago} estado={estado} prod={producto}")
    except Exception as e:
        db.session.rollback()
        print("[webhook] ERROR guardando pago:", e)

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
# ---------- Consulta de pago pendiente para dispensar ----------
@app.route("/check_payment_pendiente", methods=["GET"])
def check_pendiente():
    # Busca el primer pago aprobado y no dispensado
    pago = Pago.query.filter_by(estado="approved", dispensado=False).first()
    if not pago:
        return jsonify({"mensaje": "No hay pagos pendientes"}), 204

    # Si guardaste 'producto_id' en metadata/external_reference y también lo persistís, podés devolverlo aquí.
    # Por ahora devolvemos lo que tenemos: id_pago y nombre del producto.
    return jsonify({
        "id_pago": pago.id_pago,
        "producto": pago.producto or ""
    }), 200

# ---------- Marcar un pago como dispensado ----------
@app.route("/marcar_dispensado", methods=["POST"])
def marcar_dispensado():
    body = request.get_json(silent=True) or {}
    id_pago = str(body.get("id_pago") or "")
    if not id_pago:
        return jsonify({"error": "Falta id_pago"}), 400

    pago = Pago.query.filter_by(id_pago=id_pago).first()
    if not pago:
        return jsonify({"error": "Pago no encontrado"}), 404

    try:
        pago.dispensado = True
        # Podés opcionalmente dejar el estado como 'approved'; no es necesario cambiarlo.
        db.session.commit()
        return jsonify({"mensaje": "Pago marcado como dispensado"}), 200
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 500

@app.route("/", methods=["GET"])
def home():
    return "✅ Backend funcionando"
# ------------------------
# Entrada local (Gunicorn maneja producción)
# ------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
