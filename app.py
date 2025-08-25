# app.py
import os
import json
from datetime import datetime

from flask import Flask, request, jsonify
from flask_cors import CORS

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

import mercadopago
import paho.mqtt.client as mqtt

# -----------------------------
# Config básica
# -----------------------------
app = Flask(__name__)
CORS(app)

DB_URL = os.getenv("DATABASE_URL", "sqlite:///data.db")
if DB_URL.startswith("postgres://"):
    DB_URL = DB_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DB_URL, pool_pre_ping=True, future=True)

MP = mercadopago.SDK(os.getenv("MP_ACCESS_TOKEN", ""))

MQTT_HOST = os.getenv("MQTT_HOST")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USER = os.getenv("MQTT_USER")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD")
MQTT_TOPIC = os.getenv("MQTT_TOPIC", "disp/orden")

mqtt_client = None
if MQTT_HOST:
    mqtt_client = mqtt.Client()
    if MQTT_USER:
        mqtt_client.username_pw_set(MQTT_USER, MQTT_PASSWORD or "")
    try:
        mqtt_client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
        mqtt_client.loop_start()
        print("MQTT conectado")
    except Exception as e:
        print("MQTT no conectado:", e)

# -----------------------------
# SQL de esquema (productos + inventario + pagos)
# -----------------------------
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS public.producto (
  id              SERIAL PRIMARY KEY,
  nombre          VARCHAR(100) NOT NULL,
  precio_por_litro DOUBLE PRECISION NOT NULL DEFAULT 0,
  presentacion_litros DOUBLE PRECISION NOT NULL DEFAULT 1,
  slot_id         INTEGER NOT NULL,
  stock_litros    DOUBLE PRECISION NOT NULL DEFAULT 0,
  habilitado      BOOLEAN NOT NULL DEFAULT TRUE,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.inventario_mov (
  id            SERIAL PRIMARY KEY,
  producto_id   INTEGER NOT NULL REFERENCES public.producto(id) ON DELETE CASCADE,
  tipo          TEXT NOT NULL CHECK (tipo IN ('carga','venta','ajuste')),
  litros        DOUBLE PRECISION NOT NULL CHECK (litros > 0),
  ref_pago_id   INTEGER,
  nota          TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_inv_mov_prod ON public.inventario_mov(producto_id);

CREATE OR REPLACE FUNCTION public.fn_recalc_stock(p_prod_id INT) RETURNS VOID AS $$
BEGIN
  UPDATE public.producto p
  SET stock_litros = COALESCE((
    SELECT SUM(CASE
      WHEN m.tipo = 'carga'  THEN  m.litros
      WHEN m.tipo = 'ajuste' THEN  m.litros   -- usa litros positivos o negativos vía dos registros si querés
      WHEN m.tipo = 'venta'  THEN -m.litros
      ELSE 0 END)
    FROM public.inventario_mov m
    WHERE m.producto_id = p.id
  ), 0),
  updated_at = now()
  WHERE p.id = p_prod_id;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION public.trg_inv_mov_after_ins() RETURNS TRIGGER AS $$
BEGIN
  PERFORM public.fn_recalc_stock(NEW.producto_id);
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_inv_mov_ai ON public.inventario_mov;
CREATE TRIGGER trg_inv_mov_ai
AFTER INSERT ON public.inventario_mov
FOR EACH ROW EXECUTE FUNCTION public.trg_inv_mov_after_ins();

-- Pagos (registro de órdenes)
CREATE TABLE IF NOT EXISTS public.pago (
  id              SERIAL PRIMARY KEY,
  producto_id     INTEGER NOT NULL REFERENCES public.producto(id),
  litros          DOUBLE PRECISION NOT NULL,
  monto           DOUBLE PRECISION NOT NULL,
  estado          TEXT NOT NULL DEFAULT 'pendiente', -- pendiente | aprobado | rechazado
  mp_preference_id TEXT,
  mp_payment_id     TEXT,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

@app.before_first_request
def bootstrap():
    with engine.begin() as conn:
        conn.execute(text(SCHEMA_SQL))

# -----------------------------
# Util
# -----------------------------
def publish_mqtt(slot_id: int, litros: float):
    if not mqtt_client:
        print("MQTT no disponible, mensaje:", {"slot": slot_id, "litros": litros})
        return
    payload = json.dumps({"slot": int(slot_id), "litros": float(litros)})
    mqtt_client.publish(MQTT_TOPIC, payload, qos=1, retain=False)

# -----------------------------
# API Productos
# -----------------------------
@app.get("/api/productos")
def productos_list():
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT id, nombre, precio_por_litro, presentacion_litros,
                       slot_id, stock_litros, habilitado
                FROM public.producto
                ORDER BY id ASC;
            """)).mappings().all()
        return jsonify([dict(r) for r in rows])
    except SQLAlchemyError as e:
        return jsonify({"error": str(e)}), 500

@app.post("/api/productos")
def productos_create():
    data = request.get_json() or {}
    try:
        with engine.begin() as conn:
            r = conn.execute(text("""
                INSERT INTO public.producto(nombre, precio_por_litro, presentacion_litros, slot_id, habilitado)
                VALUES (:n, :p, :pres, :slot, :hab)
                RETURNING id;
            """), {
                "n": str(data.get("nombre","")).strip(),
                "p": float(data.get("precio_por_litro", 0)),
                "pres": float(data.get("presentacion_litros", 1)),
                "slot": int(data.get("slot_id", 1)),
                "hab": bool(data.get("habilitado", True)),
            }).first()
        return jsonify({"id": r[0]}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.patch("/api/productos/<int:pid>")
def productos_update(pid):
    data = request.get_json() or {}
    fields = []
    params = {"id": pid}
    for col in ("nombre","precio_por_litro","presentacion_litros","slot_id","habilitado"):
        if col in data:
            fields.append(f"{col} = :{col}")
            params[col] = data[col]
    if not fields:
        return jsonify({"ok": True})
    sql = f"UPDATE public.producto SET {', '.join(fields)}, updated_at=now() WHERE id=:id"
    try:
        with engine.begin() as conn:
            conn.execute(text(sql), params)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.delete("/api/productos/<int:pid>")
def productos_delete(pid):
    try:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM public.producto WHERE id=:id"), {"id": pid})
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

# -----------------------------
# API Inventario (cargas/ajustes)
# -----------------------------
@app.post("/api/inventario/cargas")
def inventario_carga():
    data = request.get_json() or {}
    prod_id = int(data.get("producto_id", 0))
    litros  = float(data.get("litros", 0))
    nota    = str(data.get("nota") or "")
    if prod_id <= 0 or litros <= 0:
        return jsonify({"error": "producto_id y litros > 0 son requeridos"}), 400
    try:
        with engine.begin() as conn:
            # valida producto
            p = conn.execute(text("SELECT id FROM public.producto WHERE id=:id"),
                             {"id": prod_id}).first()
            if not p:
                return jsonify({"error": "Producto no existe"}), 404
            conn.execute(text("""
                INSERT INTO public.inventario_mov (producto_id, tipo, litros, nota)
                VALUES (:pid, 'carga', :litros, :nota)
            """), {"pid": prod_id, "litros": litros, "nota": nota})
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

# -----------------------------
# API Pagos (MercadoPago)
# -----------------------------
@app.post("/api/pagos")
def crear_pago():
    """
    Body: { producto_id, litros }
    -> valida stock y devuelve { init_point, checkout_url, pago_id }
    """
    data = request.get_json() or {}
    pid   = int(data.get("producto_id", 0))
    litros = float(data.get("litros", 0))
    if pid <= 0 or litros <= 0:
        return jsonify({"error": "producto_id y litros > 0 son requeridos"}), 400

    try:
        with engine.begin() as conn:
            prod = conn.execute(text("""
                SELECT id, nombre, precio_por_litro, slot_id, stock_litros
                FROM public.producto WHERE id=:id AND habilitado = TRUE
            """), {"id": pid}).mappings().first()
            if not prod:
                return jsonify({"error": "Producto no habilitado / inexistente"}), 404
            if litros > float(prod["stock_litros"]):
                return jsonify({"error": "Stock insuficiente"}), 409

            monto = round(litros * float(prod["precio_por_litro"]), 2)

            # crea registro de pago (pendiente)
            pago = conn.execute(text("""
                INSERT INTO public.pago(producto_id, litros, monto, estado)
                VALUES (:pid, :litros, :monto, 'pendiente')
                RETURNING id
            """), {"pid": pid, "litros": litros, "monto": monto}).first()
            pago_id = pago[0]

        # Preferencia MP
        pref = MP.preference().create({
            "items": [{
                "title": f"{prod['nombre']} - {litros} L",
                "quantity": 1,
                "currency_id": "ARS",
                "unit_price": float(monto),
            }],
            "external_reference": str(pago_id),  # para encontrarlo en webhook
            "notification_url": request.url_root.rstrip("/") + "/webhook/mp"
        })

        pref_id = pref["response"]["id"]
        init_point = pref["response"].get("init_point") or pref["response"].get("sandbox_init_point")

        with engine.begin() as conn:
            conn.execute(text("""
                UPDATE public.pago SET mp_preference_id=:pref, updated_at=now()
                WHERE id=:id
            """), {"pref": pref_id, "id": pago_id})

        return jsonify({"pago_id": pago_id, "init_point": init_point}), 201

    except Exception as e:
        return jsonify({"error": str(e)}), 400

# Webhook MP
@app.post("/webhook/mp")
def webhook_mp():
    data = request.json or request.form or {}
    # MP notifica topic + id de pago; luego hay que consultar el pago
    topic = data.get("type") or data.get("topic")
    payment_id = data.get("data", {}).get("id") or data.get("id")

    try:
        if topic == "payment" and payment_id:
            pay = MP.payment().get(payment_id)
            status = pay["response"].get("status")
            ext_ref = pay["response"].get("external_reference")  # nuestro pago_id

            if status == "approved" and ext_ref:
                pago_id = int(ext_ref)
                # leemos datos del pago y producto
                with engine.begin() as conn:
                    row = conn.execute(text("""
                        SELECT p.id, p.producto_id, p.litros, pr.slot_id
                        FROM public.pago p
                        JOIN public.producto pr ON pr.id = p.producto_id
                        WHERE p.id=:id
                    """), {"id": pago_id}).mappings().first()
                    if not row:
                        return jsonify({"ok": True})  # nada que hacer

                    # movimiento de venta
                    conn.execute(text("""
                        INSERT INTO public.inventario_mov (producto_id, tipo, litros, ref_pago_id, nota)
                        VALUES (:pid, 'venta', :litros, :pago, 'MP OK')
                    """), {"pid": row["producto_id"], "litros": row["litros"], "pago": pago_id})

                    # actualizar pago
                    conn.execute(text("""
                        UPDATE public.pago
                        SET estado='aprobado', mp_payment_id=:mpid, updated_at=now()
                        WHERE id=:id
                    """), {"mpid": str(payment_id), "id": pago_id})

                # Publicar orden al dispensador por MQTT
                publish_mqtt(slot_id=row["slot_id"], litros=row["litros"])

        return jsonify({"ok": True})
    except Exception as e:
        # no romper el webhook
        return jsonify({"ok": False, "error": str(e)}), 200

# -----------------------------
# Salud
# -----------------------------
@app.get("/")
def root():
    return jsonify({"ok": True, "service": "dispen-easy-backend"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
