import os
import qrcode
import base64
import secrets
import io
import requests
from datetime import datetime, timedelta
from flask import (
    Flask, request, jsonify, make_response,
    render_template
)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.sql import text

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

# ============================
#   MODELOS
# ============================

class Dispenser(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(120))
    device_id = db.Column(db.String(120))
    activo = db.Column(db.Boolean, default=True)

class Producto(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    dispenser_id = db.Column(db.Integer)
    slot_id = db.Column(db.Integer)
    nombre = db.Column(db.String(120))
    precio = db.Column(db.Float)
    cantidad = db.Column(db.Float)
    habilitado = db.Column(db.Boolean, default=True)
    bundle_precios = db.Column(db.JSON)

    def to_dict(self):
        return {
            "id": self.id,
            "slot": self.slot_id,
            "nombre": self.nombre,
            "precio": self.precio,
            "cantidad": self.cantidad,
            "habilitado": self.habilitado,
            "bundle_precios": self.bundle_precios or {}
        }

class OperatorToken(db.Model):
    token = db.Column(db.String(64), primary_key=True)
    dispenser_id = db.Column(db.Integer)
    nombre = db.Column(db.String(120))
    activo = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime)
    chat_id = db.Column(db.String(80))
    mp_access_token = db.Column(db.String(255))

class OperatorProduct(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    operator_token = db.Column(db.String(64))
    product_id = db.Column(db.Integer)
    precio = db.Column(db.Float, default=None)
    bundle2 = db.Column(db.Float, default=None)
    bundle3 = db.Column(db.Float, default=None)
    habilitado = db.Column(db.Boolean, default=True)

def get_or_create_operator_product(op, prod):
    obj = OperatorProduct.query.filter_by(
        operator_token=op.token,
        product_id=prod.id
    ).first()

    if obj:
        return obj

    obj = OperatorProduct(
        operator_token=op.token,
        product_id=prod.id,
        precio=prod.precio,
        bundle2=(prod.bundle_precios or {}).get("2"),
        bundle3=(prod.bundle_precios or {}).get("3"),
        habilitado=prod.habilitado
    )
    db.session.add(obj)
    db.session.commit()
    return obj

# ============================
#   FUNCIONES AUXILIARES
# ============================

def ok_json(obj):
    return jsonify(obj)

def json_error(msg, code=400, extra=None):
    d = {"error": msg}
    if extra:
        d["detail"] = extra
    return make_response(jsonify(d), code)

def _to_int(v):
    try:
        return int(v)
    except:
        return None

def compute_total_price_ars(prod, litros):
    precio = prod.precio
    bundles = prod.bundle_precios or {}

    if str(litros) in bundles:
        return float(bundles[str(litros)])
    return float(precio) * litros

# ============================
#   ADMIN TOKENS
# ============================

def require_admin():
    admin_token = request.headers.get("x-admin-token", "")
    if admin_token != os.getenv("ADMIN_TOKEN"):
        raise Exception("No autorizado")

@app.get("/api/admin/operator_tokens")
def admin_operator_list():
    toks = OperatorToken.query.order_by(OperatorToken.created_at.desc()).all()
    return jsonify([{
        "token": t.token,
        "dispenser_id": t.dispenser_id,
        "nombre": t.nombre or "",
        "activo": bool(t.activo),
        "chat_id": t.chat_id or "",
        "created_at": t.created_at.isoformat() if t.created_at else None
    } for t in toks])


@app.post("/api/admin/operator_tokens")
def create_operator_token():
    require_admin()
    data = request.get_json(force=True) or {}

    dispenser_id = data.get("dispenser_id")
    nombre = data.get("nombre", "").strip() or "Operador"

    if not dispenser_id:
        return jsonify({"ok": False, "error": "dispenser_id requerido"}), 400

    tok = secrets.token_urlsafe(16)
    op = OperatorToken(
        token=tok,
        dispenser_id=dispenser_id,
        nombre=nombre,
        activo=True,
        created_at=datetime.utcnow(),
        chat_id=None
    )
    db.session.add(op)
    db.session.commit()

    return jsonify({"ok": True, "token": tok, "dispenser_id": dispenser_id, "nombre": nombre})


@app.put("/api/admin/operator_tokens/<token>")
def admin_operator_update(token):
    data = request.get_json(force=True) or {}
    t = OperatorToken.query.get_or_404(token)

    if "dispenser_id" in data:
        did = _to_int(data["dispenser_id"])
        if did and Dispenser.query.get(did):
            t.dispenser_id = did

    if "nombre" in data:
        t.nombre = str(data["nombre"] or "")

    if "activo" in data:
        t.activo = bool(data["activo"])

    if "chat_id" in data:
        t.chat_id = str(data["chat_id"] or "")

    db.session.commit()
    return ok_json({"ok": True})


@app.delete("/api/admin/operator_tokens/<token>")
def admin_operator_delete(token):
    t = OperatorToken.query.get_or_404(token)
    db.session.delete(t)
    db.session.commit()
    return ok_json({"ok": True})

# ============================
#  OPERADOR - PRODUCTOS
# ============================

@app.get("/api/operator/productos")
def operator_productos():
    token = (
        request.headers.get("x-operator-token")
        or request.args.get("token")
        or ""
    ).strip()

    op = OperatorToken.query.filter_by(token=token, activo=True).first()
    if not op:
        return jsonify({"ok": False, "error": "Token inválido o inactivo"}), 401

    productos = (
        Producto.query.filter_by(dispenser_id=op.dispenser_id)
        .order_by(Producto.slot_id.asc())
        .all()
    )

    lista = []
    for p in productos:
        po = get_or_create_operator_product(op, p)
        lista.append({
            "id": p.id,
            "slot": p.slot_id,
            "nombre": p.nombre,
            "cantidad": p.cantidad,
            "precio": po.precio,
            "bundle2": po.bundle2,
            "bundle3": po.bundle3,
            "habilitado": po.habilitado
        })

    return jsonify({
        "ok": True,
        "operator": {
            "token": op.token,
            "nombre": op.nombre,
            "dispenser_id": op.dispenser_id,
            "chat_id": op.chat_id
        },
        "productos": lista
    })


# ============================
#  OPERADOR - UPDATE PRODUCT
# ============================

@app.post("/api/operator/productos/update")
def operator_update_producto():
    data = request.get_json(force=True)
    token = request.headers.get("x-operator-token", "").strip()

    op = OperatorToken.query.filter_by(token=token, activo=True).first()
    if not op:
        return jsonify({"ok": False, "error": "Token inválido"}), 403

    pid = int(data.get("product_id", 0))
    prod = Producto.query.get(pid)

    if not prod or prod.dispenser_id != op.dispenser_id:
        return jsonify({"ok": False, "error": "Producto no autorizado"}), 403

    po = get_or_create_operator_product(op, prod)

    try:
        if "precio" in data and data["precio"] not in ("", None):
            po.precio = float(data["precio"])

        if "bundle2" in data:
            po.bundle2 = (
                float(data["bundle2"])
                if data["bundle2"] not in ("", None, "")
                else None
            )

        if "bundle3" in data:
            po.bundle3 = (
                float(data["bundle3"])
                if data["bundle3"] not in ("", None, "")
                else None
            )

        if "habilitado" in data:
            po.habilitado = bool(data["habilitado"])

        db.session.commit()
        return jsonify({"ok": True})

    except Exception as e:
        db.session.rollback()
        return jsonify({"ok": False, "error": str(e)})
        # ======================================================
# === REPOSICIÓN – SUMAR STOCK =========================
# ======================================================

@app.post("/api/operator/productos/reponer")
def operator_reponer():
    token = request.headers.get("x-operator-token")
    if not token:
        return jsonify({"ok": False, "error": "Falta token"}), 401

    op = OperatorToken.query.filter_by(token=token, activo=True).first()
    if not op:
        return jsonify({"ok": False, "error": "Token inválido"}), 401

    data = request.get_json(force=True)
    pid = data.get("product_id")
    litros = float(data.get("litros", 0))

    p = Producto.query.filter_by(id=pid, dispenser_id=op.dispenser_id).first()
    if not p:
        return jsonify({"ok": False, "error": "Producto no encontrado"}), 404

    p.cantidad = (p.cantidad or 0) + litros
    if not p.bundle_precios:
        p.bundle_precios = {}

    db.session.commit()

    return jsonify({"ok": True, "producto": p.to_dict()})


# ======================================================
# === RESET – SETEAR STOCK =============================
# ======================================================

@app.post("/api/operator/productos/reset")
def operator_reset():
    token = request.headers.get("x-operator-token")
    if not token:
        return jsonify({"ok": False, "error": "Falta token"}), 401

    op = OperatorToken.query.filter_by(token=token, activo=True).first()
    if not op:
        return jsonify({"ok": False, "error": "Token inválido"}), 401

    data = request.get_json(force=True)
    pid = data.get("product_id")
    litros = float(data.get("litros", 0))

    p = Producto.query.filter_by(id=pid, dispenser_id=op.dispenser_id).first()
    if not p:
        return jsonify({"ok": False, "error": "Producto no encontrado"}), 404

    p.cantidad = litros
    if not p.bundle_precios:
        p.bundle_precios = {}

    db.session.commit()
    return jsonify({"ok": True, "producto": p.to_dict()})


# ======================================================
# === VINCULACIÓN TELEGRAM =============================
# ======================================================

@app.post("/api/operator/link")
def operator_link():
    data = request.get_json(force=True, silent=True) or {}
    tok = (data.get("token") or "").strip()
    chat_id = (data.get("chat_id") or "").strip()

    if not tok or not chat_id:
        return jsonify({"error": "token y chat_id requeridos"}), 400

    op = OperatorToken.query.get(tok)
    if not op:
        return jsonify({"error": "token inválido"}), 404

    op.chat_id = chat_id
    db.session.commit()

    return jsonify({"ok": True})


@app.post("/api/operator/unlink")
def operator_unlink():
    data = request.get_json(force=True, silent=True) or {}
    tok = (data.get("token") or "").strip()

    if not tok:
        return jsonify({"error": "token requerido"}), 400

    op = OperatorToken.query.get(tok)
    if not op:
        return jsonify({"error": "token inválido"}), 404

    op.chat_id = ""
    db.session.commit()

    return jsonify({"ok": True})


# ======================================================
# === GENERAR QR DEL OPERADOR ==========================
# ======================================================

@app.get("/api/operator/productos/qr/<int:product_id>")
def operator_generar_qr(product_id):
    token = request.headers.get("x-operator-token", "").strip()

    if not token:
        return jsonify({"ok": False, "error": "Token requerido"}), 401

    op = OperatorToken.query.filter_by(token=token, activo=True).first()
    if not op:
        return jsonify({"ok": False, "error": "Operador inválido"}), 401

    prod = Producto.query.get(product_id)
    if not prod or prod.dispenser_id != op.dispenser_id:
        return jsonify({"ok": False, "error": "Producto no autorizado"}), 404

    backend_base = os.getenv("BACKEND_BASE_URL") or request.url_root.rstrip("/")
    link = f"{backend_base}/ui/seleccionar?pid={product_id}&op_token={token}"

    # QR
    buf = io.BytesIO()
    qrcode.make(link).save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()

    html = f"""
    <html><body style="text-align:center;background:#111;color:white">
      <h2>QR del producto: {prod.nombre}</h2>
      <img src="data:image/png;base64,{b64}" style="width:280px;height:280px;border-radius:12px;background:white;padding:10px">
      <p><a href="{link}">{link}</a></p>
    </body></html>
    """

    return make_response(html)


# ======================================================
# === SELECCIONAR LITROS ===============================
# ======================================================

@app.get("/ui/seleccionar")
def ui_seleccionar():
    pid = _to_int(request.args.get("pid"))
    op_token = (request.args.get("op_token") or "").strip()

    prod = Producto.query.get(pid)
    if not prod or not prod.habilitado:
        return make_response("<p>Producto no disponible</p>", 400)

    disp = Dispenser.query.get(prod.dispenser_id)
    if not disp or not disp.activo:
        return make_response("<p>Dispenser no disponible</p>", 400)

    litros_list = [1, 2, 3]
    opciones = ""

    for L in litros_list:
        precio = compute_total_price_ars(prod, L)
        opciones += f"""
        <button onclick="go({L})" style="padding:10px 20px;margin:6px;border-radius:8px;background:#007bff;color:white">
            {L} L — ${precio}
        </button><br>
        """

    html = f"""
    <html><head>
    <script>
    function go(lts){{
        fetch('/api/pagos/preferencia', {{
            method:'POST',
            headers:{{'Content-Type':'application/json'}},
            body:JSON.stringify({{product_id:{pid}, litros:lts}})
        }})
        .then(r=>r.json())
        .then(j=>{{ if(j.link) window.location=j.link; else alert('Error: '+JSON.stringify(j)); }})
    }}
    </script></head>
    <body style="background:#0b1220;color:white;text-align:center;padding-top:60px">
        <h2>{prod.nombre}</h2>
        {opciones}
    </body></html>
    """

    return make_response(html)


# ======================================================
# === PREFERENCIA PAGO OPERADOR ========================
# ======================================================

@app.post("/api/operator/pagos/preferencia")
def operator_crear_pref():
    token = request.headers.get("x-operator-token", "").strip()
    if not token:
        return json_error("Falta x-operator-token", 401)

    op = OperatorToken.query.filter_by(token=token, activo=True).first()
    if not op:
        return json_error("Operador inválido", 401)

    data = request.get_json(force=True)
    pid = _to_int(data.get("product_id"))
    litros = _to_int(data.get("litros") or 1)

    prod = Producto.query.filter_by(id=pid, dispenser_id=op.dispenser_id).first()
    if not prod:
        return json_error("Producto no autorizado", 404)

    monto = compute_total_price_ars(prod, litros)

    token_mp = op.mp_access_token
    if not token_mp:
        return json_error("Operador sin cuenta MercadoPago vinculada", 500)

    base_api = "https://api.sandbox.mercadopago.com"
    if not token_mp.startswith("TEST-"):
        base_api = "https://api.mercadopago.com"

    body = {
        "items": [{
            "title": f"{prod.nombre} {litros}L",
            "quantity": 1,
            "currency_id": "ARS",
            "unit_price": float(monto)
        }],
        "notification_url": f"{request.url_root.rstrip('/')}/api/mp/webhook",
        "auto_return": "approved",
        "back_urls": {
            "success": "/gracias?status=success",
            "failure": "/gracias?status=failure",
            "pending": "/gracias?status=pending"
        }
    }

    r = requests.post(
        f"{base_api}/checkout/preferences",
        headers={"Authorization": f"Bearer {token_mp}"},
        json=body
    )

    pref = r.json()
    link = pref.get("init_point") or pref.get("sandbox_init_point")

    return ok_json({"ok": True, "link": link})


# ======================================================
# === UPDATE ADMIN PRODUCT =============================
# ======================================================

@app.post("/api/admin/productos/update")
def admin_update_producto():
    data = request.get_json(force=True)
    pid = _to_int(data.get("product_id"))

    prod = Producto.query.get(pid)
    if not prod:
        return jsonify({"ok": False, "error": "Producto no encontrado"}), 404

    if "nombre" in data:
        prod.nombre = str(data["nombre"])

    if "precio" in data and data["precio"] not in ("", None):
        prod.precio = float(data["precio"])

    if prod.bundle_precios is None:
        prod.bundle_precios = {}

    if "bundle2" in data:
        v = data["bundle2"]
        if v not in (None, "", "null"):
            prod.bundle_precios["2"] = float(v)
        else:
            prod.bundle_precios.pop("2", None)

    if "bundle3" in data:
        v = data["bundle3"]
        if v not in (None, "", "null"):
            prod.bundle_precios["3"] = float(v)
        else:
            prod.bundle_precios.pop("3", None)

    if "habilitado" in data:
        prod.habilitado = bool(data["habilitado"])

    db.session.commit()

    return jsonify({"ok": True, "producto": prod.to_dict()})


# ======================================================
# === MAIN =============================================
# ======================================================

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
