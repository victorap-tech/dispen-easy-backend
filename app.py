import os
import io
import json
from datetime import datetime

from flask import Flask, jsonify, request, send_file
from flask_cors import CORS
from sqlalchemy import create_engine, Column, Integer, String, Float, Boolean, DateTime, JSON, text
from sqlalchemy.orm import sessionmaker, declarative_base
import mercadopago
import qrcode

# ---------------------------
# Config
# ---------------------------
DATABASE_URL   = os.getenv("DATABASE_URL")
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN")

app = Flask(__name__)
CORS(app)

# DB setup
engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()

# ---------------------------
# Modelos
# ---------------------------
class Producto(Base):
    __tablename__ = "producto"
    id         = Column(Integer, primary_key=True, index=True)
    slot_id    = Column(Integer, index=True, nullable=False, default=0, unique=True)
    nombre     = Column(String(120), nullable=False, default="")
    precio     = Column(Float, nullable=False, default=0.0)
    cantidad   = Column(Integer, nullable=False, default=1)
    habilitado = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

class Pago(Base):
    __tablename__ = "pago"
    id         = Column(Integer, primary_key=True)
    id_pago    = Column(String(64), unique=True, index=True)  # id de pago o merchant_order
    estado     = Column(String(32), default="pendiente")
    producto   = Column(String(120))
    slot_id    = Column(Integer)
    monto      = Column(Float, default=0.0)
    raw        = Column(JSON)  # payload crudo
    dispensado = Column(Boolean, default=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

Base.metadata.create_all(bind=engine)

# Asegurar columnas/índices si la tabla ya existía (Railway a veces conserva esquemas viejos)
def ensure_schema():
    with engine.begin() as conn:
        # producto
        conn.execute(text("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_indexes WHERE indexname='idx_producto_slot'
            ) THEN
                CREATE UNIQUE INDEX idx_producto_slot ON producto(slot_id);
            END IF;
        END $$;
        """))
        # pago.id_pago único (por si no existe)
        conn.execute(text("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_indexes WHERE indexname='idx_pago_id_pago'
            ) THEN
                CREATE UNIQUE INDEX idx_pago_id_pago ON pago(id_pago);
            END IF;
        END $$;
        """))

ensure_schema()

# SDK de MP
sdk = None
if MP_ACCESS_TOKEN:
    sdk = mercadopago.SDK(MP_ACCESS_TOKEN)

# ---------------------------
# Helpers
# ---------------------------
def host_base():
    # https://web-production-xxxxx.up.railway.app
    return request.host_url.rstrip("/")

def crear_preferencia(p: Producto):
    if sdk is None:
        return None, {"error": "MP_ACCESS_TOKEN no configurado"}
    # back_urls obligatorias si usás auto_return
    pref_data = {
        "items": [{
            "title": p.nombre,
            "quantity": 1,
            "unit_price": float(p.precio),
            "currency_id": "ARS",
            "description": f"Slot {p.slot_id}"
        }],
        "metadata": {"producto_id": p.id, "slot_id": p.slot_id},
        "external_reference": f"prod:{p.id}",
        "back_urls": {
            "success": f"{host_base()}/pago_exitoso",
            "pending": f"{host_base()}/pago_pendiente",
            "failure": f"{host_base()}/pago_fallido",
        },
        "notification_url": f"{host_base()}/webhook",
        "auto_return": "approved"  # <- válido porque definimos back_urls.success
    }
    pref = sdk.preference().create(pref_data)
    if pref.get("status") != 201:
        return None, {
            "error": "no se pudo crear preferencia",
            "detalle": pref.get("response")
        }
    # sandbox_init_point (test) o init_point (prod)
    resp = pref["response"]
    init_point = resp.get("init_point") or resp.get("sandbox_init_point")
    return init_point, None

def qr_png_bytes(data: str) -> bytes:
    img = qrcode.make(data)
    buff = io.BytesIO()
    img.save(buff, format="PNG")
    return buff.getvalue()

# ---------------------------
# Rutas básicas
# ---------------------------
@app.get("/")
def index():
    return jsonify({"ok": True})

# CRUD mínimo de productos
@app.get("/api/productos")
def listar_productos():
    db = SessionLocal()
    try:
        rows = db.query(Producto).order_by(Producto.slot_id).all()
        return jsonify([{
            "id": r.id, "slot_id": r.slot_id, "nombre": r.nombre,
            "precio": r.precio, "cantidad": r.cantidad, "habilitado": r.habilitado
        } for r in rows])
    finally:
        db.close()

@app.post("/api/productos/<int:slot_id>")
def crear_actualizar_producto(slot_id: int):
    body = request.get_json(force=True, silent=True) or {}
    nombre     = str(body.get("nombre", "")).strip()
    precio     = float(body.get("precio", 0) or 0)
    cantidad   = int(body.get("cantidad", 1) or 1)
    habilitado = bool(body.get("habilitado", False))

    db = SessionLocal()
    try:
        p = db.query(Producto).filter_by(slot_id=slot_id).first()
        if not p:
            p = Producto(slot_id=slot_id)
            db.add(p)
        p.nombre = nombre
        p.precio = precio
        p.cantidad = cantidad
        p.habilitado = habilitado
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()

@app.delete("/api/productos/<int:slot_id>")
def borrar_producto(slot_id: int):
    db = SessionLocal()
    try:
        p = db.query(Producto).filter_by(slot_id=slot_id).first()
        if p:
            db.delete(p)
            db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()

# Generar QR para un producto (por slot)
@app.get("/api/generar_qr/<int:slot_id>")
def generar_qr(slot_id: int):
    db = SessionLocal()
    try:
        p = db.query(Producto).filter_by(slot_id=slot_id).first()
        if not p or not p.habilitado or not p.nombre or p.precio <= 0:
            return jsonify({"error": "Producto inválido (habilitado/nombre/precio)"}), 400

init_point, err = crear_preferencia(p)
       if err:
    # burbujea la respuesta cruda de MP si existe
         detalle = err.get("detalle")
         return jsonify({
        "error": err.get("error", "mp_error"),
        "detalle": detalle
    }), 400

        png = qr_png_bytes(init_point)
        return send_file(io.BytesIO(png), mimetype="image/png")
    finally:
        db.close()

# ---------------------------
# Webhook Mercado Pago
# ---------------------------
@app.post("/webhook")
def webhook():
    """
    Recibe eventos de MP.
    Puede ser type=payment o type=merchant_order.
    Guardamos registro y, si es payment, levantamos info de pago.
    """
    db = SessionLocal()
    try:
        payload = request.get_json(silent=True) or {}
        type_ = payload.get("type")
        data = payload.get("data") or {}
        ref_id = str(data.get("id") or payload.get("id") or "UNKNOWN")

        # Evitar duplicados
        ya = db.query(Pago).filter_by(id_pago=ref_id).first()
        if ya:
            return jsonify({"ok": True, "dup": True})

        estado = "pendiente"
        monto = 0.0
        slot = None
        prod_name = None

        # Si es pago, intentar consultar detalles del pago
        if type_ == "payment" and sdk:
            try:
                pay = sdk.payment().get(ref_id)
                pr = pay.get("response") or {}
                estado = pr.get("status") or "desconocido"
                monto = float(pr.get("transaction_amount") or 0)
                md = pr.get("metadata") or {}
                slot = md.get("slot_id")
                prod_name = md.get("producto_id")
            except Exception:
                pass

        reg = Pago(
            id_pago=ref_id,
            estado=estado,
            producto=str(prod_name) if prod_name else None,
            slot_id=int(slot) if slot is not None else None,
            monto=monto,
            raw=payload,
            dispensado=False
        )
        db.add(reg)
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()

# Rutas de retorno (opcionales, para que no den 404 si el usuario vuelve)
@app.get("/pago_exitoso")
def pago_ok():
    return "Pago exitoso, ¡gracias!"

@app.get("/pago_pendiente")
def pago_pending():
    return "Pago pendiente."

@app.get("/pago_fallido")
def pago_fail():
    return "Pago fallido."

# ---------------------------
# Run
# ---------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
