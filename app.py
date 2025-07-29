from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
import os
import datetime
import paho.mqtt.client as mqtt
import requests
import json
from decimal import Decimal

# --- Configuración de la Aplicación Flask ---
app = Flask(__name__)
CORS(app) # Habilita CORS para que el ESP32 o apps externas puedan consumirlo

# --- Configuración de la Base de Datos ---
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get("DATABASE_URL", "sqlite:///./pagos.db")
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app) # Inicializa la extensión SQLAlchemy

# --- Configuración de Mercado Pago ---
MP_ACCESS_TOKEN = os.environ.get("MP_ACCESS_TOKEN")
if not MP_ACCESS_TOKEN:
    print("ADVERTENCIA: MP_ACCESS_TOKEN no configurado. Las operaciones de Mercado Pago no funcionarán.")

FRONTEND_URL = os.environ.get("FRONTEND_URL", "https://dispen-easy-web-production.up.railway.app/")

# --- Configuración MQTT ---
MQTT_BROKER = os.environ.get("MQTT_BROKER_HOST", "c9b4a2b821ec4e87b10ed8e0ace8e4ee.s1.eu.hivemq.cloud")
MQTT_PORT = int(os.environ.get("MQTT_BROKER_PORT", 8883))
MQTT_USERNAME = os.environ.get("MQTT_BROKER_USERNAME", "Victor")
MQTT_PASSWORD = os.environ.get("MQTT_BROKER_PASSWORD", "Dispeneasy25")

mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
mqtt_client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print(f"Conectado exitosamente al broker MQTT: {MQTT_BROKER}")
        client.subscribe("dispensador/status")
    else:
        print(f"Fallo la conexión al broker MQTT, código de retorno: {rc}: {mqtt.connack_string(rc)}")

def on_message(client, userdata, msg):
    print(f"Mensaje MQTT recibido - Tópico: {msg.topic}, Mensaje: {msg.payload.decode()}")
    if msg.topic == "dispensador/status":
        try:
            status_data = json.loads(msg.payload.decode())
            id_pago = status_data.get("id_pago")
            estado_dispensador = status_data.get("estado")

            if id_pago and estado_dispensador:
                with app.app_context(): # Es necesario para usar db.session fuera del contexto de una petición
                    pago = Pago.query.filter_by(id_pago_mp=str(id_pago)).first()
                    if pago:
                        if estado_dispensador == "DISPENSADO":
                            if not pago.dispensado:
                                pago.dispensado = True
                                db.session.commit()
                                print(f"Pago {id_pago} marcado como DISPENSADO en la DB.")
                        elif estado_dispensador == "ERROR":
                            print(f"Error de dispensación para el pago {id_pago}.")
                    else:
                        print(f"Pago {id_pago} no encontrado en la DB.")
            else:
                print("Datos de estado MQTT incompletos (falta id_pago o estado).")
        except json.JSONDecodeError:
            print("Error: Mensaje MQTT no es un JSON válido.")
        except Exception as e:
            print(f"Error procesando mensaje MQTT: {e}")

mqtt_client.on_connect = on_connect
mqtt_client.on_message = on_message

try:
    mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
    mqtt_client.loop_start()
except Exception as e:
    print(f"No se pudo conectar al broker MQTT: {e}")

# --- Modelos de Base de Datos (SQLAlchemy) ---
class Producto(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(100), nullable=False)
    cantidad_ml = db.Column(db.Integer, nullable=False)
    precio = db.Column(db.Numeric(10, 2), nullable=False)

    def __repr__(self):
        return f'<Producto {self.nombre} ({self.cantidad_ml}ml)>'

class Pago(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    id_pago_mp = db.Column(db.String(255), unique=True, nullable=False)
    estado = db.Column(db.String(50), nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.datetime.now)
    dispensado = db.Column(db.Boolean, default=False)
    producto_id = db.Column(db.Integer, db.ForeignKey('producto.id'))
    producto = db.relationship('Producto', backref='pagos')

    def __repr__(self):
        return f'<Pago {self.id_pago_mp} - {self.estado}>'

# --- Función de Inicialización de la Base de Datos (nueva forma) ---
# Esta función se llamará cuando la aplicación se inicie, garantizando el contexto de la app.
def initialize_database():
    with app.app_context():
        db.create_all()
        print("Tablas de la base de datos creadas/verificadas.")
        # Opcional: Añadir algunos productos de ejemplo si la tabla está vacía
        if Producto.query.count() == 0:
            print("Añadiendo productos de ejemplo...")
            p1 = Producto(nombre="Agua Mineral", cantidad_ml=500, precio=Decimal("150.00"))
            p2 = Producto(nombre="Jugo Naranja", cantidad_ml=300, precio=Decimal("200.50"))
            p3 = Producto(nombre="Gaseosa Cola", cantidad_ml=400, precio=Decimal("250.75"))
            db.session.add_all([p1, p2, p3])
            db.session.commit()
            print("Productos de ejemplo añadidos.")

# --- Rutas de la API ---

@app.route('/')
def home():
    return jsonify({"message": "Backend Dispen-Easy operativo. Accede a /api/productos para la API."})

@app.route('/api/productos', methods=['GET', 'POST'])
def handle_productos():
    if request.method == 'GET':
        productos = Producto.query.all()
        productos_data = [
            {
                'id': p.id,
                'nombre': p.nombre,
                'cantidad_ml': p.cantidad_ml,
                'precio': float(p.precio)
            } for p in productos
        ]
        return jsonify(productos_data)
    
    elif request.method == 'POST':
        data = request.get_json()
        nombre = data.get('nombre')
        cantidad_ml = data.get('cantidad_ml')
        precio = data.get('precio')

        if not all([nombre, cantidad_ml, precio is not None]):
            return jsonify({"error": "Faltan datos del producto"}), 400
        
        try:
            precio_decimal = Decimal(str(precio))
            nuevo_producto = Producto(nombre=nombre, cantidad_ml=cantidad_ml, precio=precio_decimal)
            db.session.add(nuevo_producto)
            db.session.commit()
            return jsonify({"status": "success", "message": "Producto añadido", "id": nuevo_producto.id, "nombre": nuevo_producto.nombre, "cantidad_ml": nuevo_producto.cantidad_ml, "precio": float(nuevo_producto.precio)}), 201
        except Exception as e:
            db.session.rollback()
            print(f"Error al añadir producto: {e}")
            return jsonify({"error": "Error interno al añadir producto"}), 500

@app.route('/api/generar_qr/<int:producto_id>', methods=['POST'])
def generar_qr(producto_id):
    if not MP_ACCESS_TOKEN:
        return jsonify({"status": "error", "message": "MP_ACCESS_TOKEN no configurado en el backend"}), 500

    producto = Producto.query.get(producto_id)
    if not producto:
        return jsonify({"status": "error", "message": "Producto no encontrado"}), 404

    external_reference = f"pago-{producto.id}-{datetime.datetime.now().strftime('%Y%m%d%H%M%S%f')}"

    preference_data = {
        "items": [
            {
                "title": f"{producto.nombre} ({producto.cantidad_ml}ml)",
                "quantity": 1,
                "unit_price": float(producto.precio)
            }
        ],
        "external_reference": external_reference,
        "notification_url": f"{request.url_root}webhook_mp",
        "back_urls": {
            "success": FRONTEND_URL,
            "pending": FRONTEND_URL,
            "failure": FRONTEND_URL
        },
        "auto_return": "approved"
    }

    headers = {
        "Authorization": f"Bearer {MP_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }

    try:
        mp_response = requests.post(
            "https://api.mercadopago.com/checkout/preferences",
            headers=headers,
            json=preference_data
        )
        mp_response.raise_for_status()
        preference = mp_response.json()
        
        qr_url = preference.get('init_point') 
        if qr_url:
            return jsonify({
                "status": "success",
                "message": "QR generado exitosamente",
                "qr_data": qr_url,
                "id_pago_mp": preference.get('id')
            })
        else:
            return jsonify({"status": "error", "message": "No se obtuvo 'init_point' de Mercado Pago"}), 500

    except requests.exceptions.RequestException as e:
        print(f"Error al conectar con la API de Mercado Pago: {e}")
        return jsonify({"status": "error", "message": f"Error de conexión con Mercado Pago: {e}"}), 500
    except Exception as e:
        print(f"Error inesperado al generar QR: {e}")
        return jsonify({"status": "error", "message": "Error inesperado al generar QR"}), 500

@app.route('/webhook_mp', methods=['POST'])
def webhook_mp():
    data = request.get_json()
    print(f"[WEBHOOK MP] Datos recibidos: {json.dumps(data, indent=2)}")

    if data.get('type') == 'payment':
        payment_id = data.get('data', {}).get('id')
        if not payment_id:
            print("[WEBHOOK MP] ID de pago no encontrado en la notificación.")
            return jsonify({"status": "error", "message": "ID de pago no encontrado"}), 400

        headers = {
            "Authorization": f"Bearer {MP_ACCESS_TOKEN}"
        }
        try:
            payment_response = requests.get(
                f"https://api.mercadopago.com/v1/payments/{payment_id}",
                headers=headers
            )
            payment_response.raise_for_status()
            payment_info = payment_response.json()
            
            status = payment_info.get('status')
            external_reference = payment_info.get('external_reference')
            
            print(f"[WEBHOOK MP] Detalles del pago {payment_id}: Estado={status}, Ref. Externa={external_reference}")

            with app.app_context():
                pago_existente = Pago.query.filter_by(id_pago_mp=str(payment_id)).first()
                
                if not pago_existente:
                    try:
                        p_id_str = external_reference.split('-')[1]
                        producto_referenciado = Producto.query.get(int(p_id_str))
                    except (AttributeError, IndexError, ValueError):
                        producto_referenciado = None
                        print(f"No se pudo extraer producto_id de external_reference: {external_reference}")

                    nuevo_pago = Pago(
                        id_pago_mp=str(payment_id),
                        estado=status,
                        timestamp=datetime.datetime.now(),
                        dispensado=False,
                        producto=producto_referenciado
                    )
                    db.session.add(nuevo_pago)
                    db.session.commit()
                    print(f"Nuevo pago {payment_id} registrado con estado {status}.")
                else:
                    if pago_existente.estado != status:
                        pago_existente.estado = status
                        db.session.commit()
                        print(f"Estado de pago {payment_id} actualizado a {status}.")
                    else:
                        print(f"Pago {payment_id} ya registrado con estado {status}. No se requiere actualización.")

                if status == "approved" and (not pago_existente or not pago_existente.dispensado):
                    print(f"Pago {payment_id} aprobado y no dispensado. Enviando comando MQTT.")
                    payload = json.dumps({
                        "id_pago": str(payment_id),
                        "comando": "DISPENSAR",
                        "producto_id": producto_referenciado.id if producto_referenciado else "N/A",
                        "cantidad_ml": producto_referenciado.cantidad_ml if producto_referenciado else "N/A"
                    })
                    mqtt_client.publish("dispensador/comando", payload)
                    print(f"Comando DISPENSAR enviado a dispensador/comando para pago {payment_id}.")
                elif status == "approved" and pago_existente and pago_existente.dispensado:
                    print(f"Pago {payment_id} aprobado pero ya estaba marcado como dispensado.")

        except requests.exceptions.RequestException as e:
            print(f"[WEBHOOK MP] Error al obtener detalles del pago desde MP: {e}")
            return jsonify({"status": "error", "message": f"Error al consultar pago en MP: {e}"}), 500
        except Exception as e:
            print(f"[WEBHOOK MP] Error inesperado al procesar webhook: {e}")
            return jsonify({"status": "error", "message": "Error interno al procesar webhook"}), 500

    return jsonify({"status": "ok"}), 200

# --- Punto de entrada principal de la aplicación ---
if __name__ == '__main__':
    # Esto se ejecuta solo cuando corres 'python app.py' directamente
    # Asegura que las tablas se creen e inicien los productos de ejemplo en desarrollo
    initialize_database()
    app.run(host='0.0.0.0', port=os.environ.get('PORT', 5000), debug=True)
with app.app_context():
    initialize_database()
