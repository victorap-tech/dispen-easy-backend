from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
import requests, qrcode, io, base64, os, ssl, json
import traceback
import paho.mqtt.client as mqtt

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

# --- Helpers MQTT ---
import os, ssl, json
import paho.mqtt.client as mqtt

def _mqtt_client():
    broker   = os.getenv("MQTT_BROKER")                 # p.ej. c9b4a2b8...s1.eu.hivemq.cloud
    port     = int(os.getenv("MQTT_PORT", "8883"))      # 8883 (TLS)
    user     = os.getenv("MQTT_USER")
    pwd      = os.getenv("MQTT_PASS")
    client_id = os.getenv("MQTT_CLIENT_ID", "dispen-easy-backend")

    if not all([broker, user, pwd]):
        raise RuntimeError("Faltan variables MQTT_BROKER / MQTT_USER / MQTT_PASS")

    client = mqtt.Client(client_id=client_id, clean_session=True)
    client.username_pw_set(user, pwd)
    # TLS seguro
    client.tls_set(tls_version=ssl.PROTOCOL_TLS, cert_reqs=ssl.CERT_REQUIRED)
    client.tls_insecure_set(False)
    return client, broker, port

def mqtt_publish(payload: dict, topic: str = None, qos: int = 1, retain: bool = False):
    if topic is None:
        topic = os.getenv("MQTT_TOPIC", "dispen-easy/dispensar")

    client, broker, port = _mqtt_client()
    client.connect(broker, port, keepalive=int(os.getenv("MQTT_KEEPALIVE", "60")))
    client.loop_start()
    try:
        info = client.publish(topic, json.dumps(payload), qos=qos, retain=retain)
        info.wait_for_publish(timeout=5)
    finally:
        client.loop_stop()
        client.disconnect()

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

#-------Helper slotid-----
from sqlalchemy import text

def ensure_slot_id_column():
    try:
        res = db.session.execute(text("PRAGMA table_info(producto)"))
        cols = [row[1] for row in res]
        if "slot_id" not in cols:
            db.session.execute(
                text("ALTER TABLE producto ADD COLUMN slot_id INTEGER NOT NULL DEFAULT 0")
            )
            db.session.commit()
            print("[DB] Columna slot_id agregada")
        else:
            print("[DB] Columna slot_id ya existe")
    except Exception as e:
        print("[DB] Error creando columna slot_id:", e)
        db.session.rollback()

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
    nombre = db.Column(db.String(120), nullable=False)
    precio = db.Column(db.Float, nullable=False)
    cantidad = db.Column(db.Integer, nullable=False, default=1)
    slot_id = db.Column(db.Integer, nullable=False, default=0)  # <--- NUEVO

    def to_dict(self):
        return {
            "id": self.id,
            "nombre": self.nombre,
            "precio": self.precio,
            "cantidad": self.cantidad,
            "slot_id": self.slot_id,            # <--- incluir en API
        }


# Crear tablas si no existen
with app.app_context():
    db.create_all()
    ensure_slot_id_column()
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

# ==== CRUD Productos =========================================================

def _producto_to_dict(p: "Producto"):
    return {
        "id": p.id,
        "nombre": p.nombre,
        "precio": p.precio,
        "cantidad": p.cantidad,
        "slot_id": p.slot_id,
    }

@app.route("/api/productos", methods=["GET"])
def listar_productos():
    productos = Producto.query.order_by(Producto.id.asc()).all()
    return jsonify([_producto_to_dict(p) for p in productos])

@app.route("/api/productos/<int:id>", methods=["GET"])
def obtener_producto(id):
    p = Producto.query.get(id)
    if not p:
        return jsonify({"error": "Producto no encontrado"}), 404
    return jsonify(_producto_to_dict(p))

@app.route("/api/productos", methods=["POST"])
def crear_producto():
    data = request.get_json(force=True) or {}

    nombre   = (data.get("nombre") or "").strip()
    precio   = float(data.get("precio", 0))
    cantidad = int(data.get("cantidad", 1))

    if not nombre:
        return jsonify({"error": "nombre es requerido"}), 400
    if precio <= 0:
        return jsonify({"error": "precio debe ser > 0"}), 400
    if cantidad <= 0:
        return jsonify({"error": "cantidad debe ser > 0"}), 400

    # slot_id opcional; si no viene, se asigna automáticamente (0..5)
    slot_id = data.get("slot_id")
    if slot_id is None:
        existentes = Producto.query.order_by(Producto.id.asc()).all()
        slot_id = len(existentes) % 6
    else:
        try:
            slot_id = int(slot_id)
        except Exception:
            return jsonify({"error": "slot_id inválido"}), 400
        if not (0 <= slot_id <= 5):
            return jsonify({"error": "slot_id fuera de rango (0..5)"}), 400

    p = Producto(nombre=nombre, precio=precio, cantidad=cantidad, slot_id=slot_id)
    db.session.add(p)
    db.session.commit()
    return jsonify(_producto_to_dict(p)), 201

@app.route("/api/productos/<int:id>", methods=["PUT", "PATCH"])
def actualizar_producto(id):
    p = Producto.query.get(id)
    if not p:
        return jsonify({"error": "Producto no encontrado"}), 404

    data = request.get_json(force=True) or {}

    if "nombre" in data:
        nombre = (data.get("nombre") or "").strip()
        if not nombre:
            return jsonify({"error": "nombre no puede ser vacío"}), 400
        p.nombre = nombre

    if "precio" in data:
        try:
            precio = float(data.get("precio"))
        except Exception:
            return jsonify({"error": "precio inválido"}), 400
        if precio <= 0:
            return jsonify({"error": "precio debe ser > 0"}), 400
        p.precio = precio

    if "cantidad" in data:
        try:
            cantidad = int(data.get("cantidad"))
        except Exception:
            return jsonify({"error": "cantidad inválida"}), 400
        if cantidad <= 0:
            return jsonify({"error": "cantidad debe ser > 0"}), 400
        p.cantidad = cantidad

    if "slot_id" in data:
        try:
            slot_id = int(data.get("slot_id"))
        except Exception:
            return jsonify({"error": "slot_id inválido"}), 400
        if not (0 <= slot_id <= 5):
            return jsonify({"error": "slot_id fuera de rango (0..5)"}), 400
        p.slot_id = slot_id

    db.session.commit()
    return jsonify(_producto_to_dict(p))

@app.route("/api/productos/<int:id>", methods=["DELETE"])
def eliminar_producto(id):
    p = Producto.query.get(id)
    if not p:
        return jsonify({"error": "Producto no encontrado"}), 404
    db.session.delete(p)
    db.session.commit()
    return jsonify({"mensaje": "Producto eliminado"})
# ============================================================================
# -----------------------------------------
# Generar QR de pago (Mercado Pago)
# -----------------------------------------
@app.route("/api/generar_qr/<int:id>", methods=["GET"])
def generar_qr(id):
    producto = Producto.query.get(id)
    if not producto:
        return jsonify({"error": "Producto no encontrado"}), 404

    token = os.getenv("MP_ACCESS_TOKEN")
    if not token:
        return jsonify({"error": "Falta MP_ACCESS_TOKEN"}), 500

    titulo_visible = producto.nombre  # podés sumar marca si querés

    payload = {
        "items": [{
            "title": titulo_visible,
            "quantity": 1,
            "unit_price": float(producto.precio),
        }],
        "description": producto.nombre,
        "additional_info": {"items": [{"title": producto.nombre}]},
        "metadata": {
            "product_id": producto.id,
            "slot_id": producto.slot_id,      # <--- clave para el ESP
        },
        "external_reference": f"prod:{producto.id}",
        "notification_url": "https://web-production-e7d2.up.railway.app/webhook",
        "back_urls": {
            "success": "https://dispen-easy-web-production.up.railway.app/",
            "pending": "https://dispen-easy-web-production.up.railway.app/",
            "failure": "https://dispen-easy-web-production.up.railway.app/",
        },
        "auto_return": "approved",
    }

    # … POST a MP como ya lo tenés …

    # 4) Crear preferencia
    resp = requests.post(url, headers=headers, json=payload, timeout=12)
    if resp.status_code != 201:
        try:
            detalle = resp.json()
        except Exception:
            detalle = resp.text
        print("MP error:", resp.status_code, detalle, flush=True)
        return jsonify({"error": "No se pudo generar link de pago", "detalle": detalle}), 502

    # 5) Link de pago
    link = resp.json().get("init_point")

    # 6) Generar QR (PNG Base64)
    import qrcode, io, base64
    img = qrcode.make(link)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    qr_base64 = base64.b64encode(buf.getvalue()).decode("utf-8")

    return jsonify({"qr_base64": qr_base64, "link": link}), 200

# -----------------------------------------
# Webhook Mercado Pago (con MQTT)
# -----------------------------------------
@app.route("/webhook", methods=["POST"])
def webhook():
    # 1) Cuerpo crudo
    raw = request.get_json(silent=True) or {}
    print("[webhook] raw =", raw, flush=True)

    # 2) Token MP
    token = os.getenv("MP_ACCESS_TOKEN")
    if not token:
        print("[webhook] Falta MP_ACCESS_TOKEN", flush=True)
        return jsonify({"error": "missing token"}), 500

    # 3) Resolver payment_id
    payment_id = None

    # 3.a) Formato nuevo {"data": {"id": ...}}
    if isinstance(raw.get("data"), dict) and raw["data"].get("id"):
        payment_id = str(raw["data"]["id"])

    # 3.b) Formato clásico {resource, topic}
    if not payment_id and raw.get("resource") and raw.get("topic"):
        topic = raw["topic"]
        resource = str(raw["resource"]).rstrip("/")

        # /v1/payments/{id}
        if topic == "payment" and "/payments/" in resource:
            payment_id = resource.split("/")[-1]

        # /merchant_orders/{id} -> buscar último payment
        if not payment_id and topic == "merchant_order" and "/merchant_orders/" in resource:
            mo_id = resource.split("/")[-1]
            try:
                r_mo = requests.get(
                    f"https://api.mercadolibre.com/merchant_orders/{mo_id}",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=12,
                )
                mo = r_mo.json()
                payments = mo.get("payments", []) or []
                if payments:
                    # último payment
                    payment_id = str(payments[-1].get("id"))
                print(f"[webhook] MO resp {r_mo.status_code}", flush=True)
            except Exception as e:
                print("[webhook] Error consultando merchant_orders:", e, flush=True)

    if not payment_id:
        print("[webhook] sin payment_id -> ignored", flush=True)
        return jsonify({"status": "ignored"}), 200

    # 4) Traer detalle del pago
    try:
        r_pay = requests.get(
            f"https://api.mercadopago.com/v1/payments/{payment_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=12,
        )
        pay = r_pay.json()
        print("[webhook] payment:", r_pay.status_code,
              {"id": pay.get("id"), "status": pay.get("status")}, flush=True)
    except Exception as e:
        print("[webhook] error consultando payment:", e, flush=True)
        return jsonify({"status": "ok"}), 200  # que MP no reintente infinito

    # 5) Estado + metadata
    estado = (pay.get("status") or "").lower()
    meta = pay.get("metadata") or {}

    # nombre del producto (para tu auditoría)
    producto_nombre = (
        meta.get("producto_nombre")
        or pay.get("description")
        or (pay.get("additional_info") or {}).get("items", [{}])[0].get("title")
        or "Desconocido"
    )

    # id real del producto (en tu DB) y slot_id (si vino en el QR)
    producto_id_real = meta.get("producto_id")
    slot_id = meta.get("slot_id")

    # 6) Resolver slot_id si no vino en metadata
    if slot_id is None:
        # 6.a) Si tengo product_id, pruebo buscar en DB (requiere campo slot_id en Producto)
        if producto_id_real:
            try:
                prod = Producto.query.get(producto_id_real)
                if prod and hasattr(prod, "slot_id") and prod.slot_id is not None:
                    slot_id = int(prod.slot_id)
            except Exception as e:
                print("[webhook] Error buscando Producto en DB:", e, flush=True)

        # 6.b) Fallback por posición en la lista (índice % 6)
        if slot_id is None and producto_id_real:
            try:
                productos = Producto.query.order_by(Producto.id.asc()).all()
                ids = [p.id for p in productos]
                if producto_id_real in ids:
                    slot_id = ids.index(producto_id_real) % 6
            except Exception as e:
                print("[webhook] Error resolviendo slot por posición:", e, flush=True)

    print(f"[webhook] slot_id resuelto: {slot_id}", flush=True)

    # 7) Guardar/actualizar en DB (tabla Pagos)
    try:
        reg = Pago.query.filter_by(id_pago=str(payment_id)).first()
        if reg:
            reg.estado = estado
            reg.producto = producto_nombre
        else:
            reg = Pago(
                id_pago=str(payment_id),
                estado=estado,
                producto=producto_nombre,
                dispensado=False,
            )
            db.session.add(reg)
        db.session.commit()
        print(f"[webhook] Pago guardado/actualizado id={payment_id} estado={estado} prod={producto_nombre}",
              flush=True)
    except Exception as e:
        db.session.rollback()
        print("[webhook] ERROR guardando pago:", e, flush=True)

    # 8) Publicar MQTT si aprobado y sabemos a qué salida va
    try:
        if estado == "approved" and slot_id is not None:
            mqtt_publish({
                "comando": "activar",
                "slot_id": int(slot_id),
                "pago_id": str(payment_id),
            })
            print("[webhook] MQTT publicado OK", flush=True)
        else:
            print(f"[webhook] no MQTT (estado={estado}, slot_id={slot_id})", flush=True)
    except Exception as e:
        print("[webhook] Error publicando MQTT:", e, flush=True)

    # 9) Siempre 200 a MP
    return jsonify({"status": "ok"}), 200
    
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


# ── Consultar si hay un pago APROBADO y NO dispensado ──────────────────────────
@app.route('/api/check_payment_pendiente', methods=['GET'])
def check_payment_pendiente():
    p = (Pago.query
            .filter_by(estado='approved', dispensado=False)
            .order_by(Pago.id.desc())
            .first())
    if not p:
        # 204 = no hay contenido; el front puede interpretarlo como "no hay nada aún"
        return jsonify({'mensaje': 'No hay pagos pendientes'}), 204

    return jsonify({
        'id': p.id,
        'id_pago': p.id_pago,
        'producto': p.producto,
        'estado': p.estado
    }), 200


# ── Marcar como DISPENSADO (lo llama el front cuando ya liberó el producto) ────
@app.route('/api/marcar_dispensado', methods=['POST'])
def marcar_dispensado():
    data = request.get_json(silent=True) or {}
    id_pago = data.get('id_pago')

    if not id_pago:
        return jsonify({'error': 'id_pago requerido'}), 400

    p = Pago.query.filter_by(id_pago=id_pago, estado='approved', dispensado=False).first()
    if not p:
        return jsonify({'error': 'Pago no encontrado o ya dispensado'}), 404

    p.dispensado = True
    db.session.commit()
    return jsonify({'mensaje': 'Pago marcado como dispensado'}), 200
    
#----api test-mqtt------
@app.route("/api/test-mqtt", methods=["POST"])
def test_mqtt():
    try:
        data = request.get_json() or {}
        producto = data.get("producto", "prueba")
        pago_id = data.get("pago_id", "test123")

        mqtt_payload = {
            "comando": "dispensar",
            "producto": producto,
            "pago_id": pago_id
        }

        resultado = publicar_mqtt(
            topic=os.getenv("MQTT_TOPIC", "dispen-easy/pagos"),
            mensaje=json.dumps(mqtt_payload)
        )

        return jsonify({
            "status": "ok",
            "mensaje": "MQTT enviado",
            "resultado": resultado
        }), 200
    except Exception as e:
        return jsonify({"status": "error", "detalle": str(e)}), 500


@app.route("/", methods=["GET"])
def home():
    return "✅ Backend funcionando"
# ------------------------
# Entrada local (Gunicorn maneja producción)
# ------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
