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
app = Flask(__name__) # Ya NO especificamos static_folder ni template_folder
CORS(app) # Habilita CORS para que el ESP32 o apps externas puedan consumirlo

# --- Configuración de la Base de Datos ---
# Para Railway (producción), usará DATABASE_URL que Railway inyectará para PostgreSQL.
# Para desarrollo local, usará 'sqlite:///./pagos.db'
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get("DATABASE_URL", "sqlite:///./pagos.db")
# Deshabilita el seguimiento de modificaciones de objetos SQLAlchemy para ahorrar memoria
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app) # Inicializa la extensión SQLAlchemy

# --- Configuración de Mercado Pago ---
MP_ACCESS_TOKEN = os.environ.get("MP_ACCESS_TOKEN")
if not MP_ACCESS_TOKEN:
    print("ADVERTENCIA: MP_ACCESS_TOKEN no configurado. Las operaciones de Mercado Pago no funcionarán.")
    # raise ValueError("MP_ACCESS_TOKEN no configurado en las variables de entorno.") # Descomentar para producción

# URL del frontend para CORS (obtenida de variables de entorno o localhost para desarrollo)
# Esta URL es crucial para la redirección de Mercado Pago después del pago.
FRONTEND_URL = os.environ.get("FRONTEND_URL", "https://dispen-easy-web-production.up.railway.app/") # Asume que tu frontend corre en el puerto 3000 por defecto (ej. React)

# --- Configuración MQTT ---
MQTT_BROKER = os.environ.get("MQTT_BROKER_HOST", "c9b4a2b821ec4e87b10ed8e0ace8e4ee.s1.eu.hivemq.cloud") # Ej: xxxxx.s1.eu.hivemq.cloud
MQTT_PORT = int(os.environ.get("MQTT_BROKER_PORT", 8883)) # Ej: 8883 para SSL/TLS, 1883 para TCP
MQTT_USERNAME = os.environ.get("MQTT_BROKER_USERNAME", "Victor")
MQTT_PASSWORD = os.environ.get("MQTT_BROKER_PASSWORD", "Dispeneasy25")

# Cliente MQTT
mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1) # Especifica la versión de la API
mqtt_client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print(f"Conectado exitosamente al broker MQTT: {MQTT_BROKER}")
        # Suscribirse a tópicos después de la conexión exitosa
        client.subscribe("dispensador/status") # Para que el ESP32 envíe el estado de dispensación
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
                            if not pago.dispensado: # Marcar solo si no estaba dispensado
                                pago.dispensado = True
                                db.session.commit()
                                print(f"Pago {id_pago} marcado como DISPENSADO en la DB.")
                        elif estado_dispensador == "ERROR":
                            # Aquí puedes manejar un error de dispensación, quizás reversar el pago, notificar, etc.
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
    # Si usas SSL/TLS (puerto 8883 o similar para HiveMQ Cloud)
    # mqtt_client.tls_set(tls_version=mqtt.ssl.PROTOCOL_TLS)
    mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
    mqtt_client.loop_start() # Inicia el bucle en un hilo separado
except Exception as e:
    print(f"No se pudo conectar al broker MQTT: {e}")

# --- Modelos de Base de Datos (SQLAlchemy) ---
class Producto(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(100), nullable=False)
    cantidad_ml = db.Column(db.Integer, nullable=False)
    precio = db.Column(db.Numeric(10, 2), nullable=False) # Decimal para precisión monetaria

    def __repr__(self):
        return f'<Producto {self.nombre} ({self.cantidad_ml}ml)>'

class Pago(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    id_pago_mp = db.Column(db.String(255), unique=True, nullable=False) # ID del pago de Mercado Pago
    estado = db.Column(db.String(50), nullable=False) # Ej: "approved", "pending", "rejected"
    timestamp = db.Column(db.DateTime, default=datetime.datetime.now)
    dispensado = db.Column(db.Boolean, default=False) # True si ya se dispensó el producto
    producto_id = db.Column(db.Integer, db.ForeignKey('producto.id')) # Relación con Producto
    producto = db.relationship('Producto', backref='pagos') # Permite acceder al producto desde el pago

    def __repr__(self):
        return f'<Pago {self.id_pago_mp} - {self.estado}>'

# --- Inicialización de la Base de Datos ---
@app.before_first_request
def create_tables():
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

# Ruta principal de la API (puedes poner un mensaje simple para verificar que funciona)
@app.route('/')
def home():
    return jsonify({"message": "Backend Dispen-Easy operativo. Accede a /api/productos para la API."})

# Ruta para obtener y AÑADIR productos (GET y POST)
@app.route('/api/productos', methods=['GET', 'POST'])
def handle_productos():
    if request.method == 'GET':
        productos = Producto.query.all()
        productos_data = [
            {
                'id': p.id,
                'nombre': p.nombre,
                'cantidad_ml': p.cantidad_ml,
                'precio': float(p.precio) # Asegúrate de que el precio sea flotante para JSON
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
            # Convertir precio a Decimal antes de guardar en la DB
            precio_decimal = Decimal(str(precio))
            nuevo_producto = Producto(nombre=nombre, cantidad_ml=cantidad_ml, precio=precio_decimal)
            db.session.add(nuevo_producto)
            db.session.commit()
            return jsonify({"status": "success", "message": "Producto añadido", "id": nuevo_producto.id, "nombre": nuevo_producto.nombre, "cantidad_ml": nuevo_producto.cantidad_ml, "precio": float(nuevo_producto.precio)}), 201
        except Exception as e:
            db.session.rollback()
            print(f"Error al añadir producto: {e}")
            return jsonify({"error": "Error interno al añadir producto"}), 500

# Ruta para generar código QR de Mercado Pago
@app.route('/api/generar_qr/<int:producto_id>', methods=['POST'])
def generar_qr(producto_id):
    if not MP_ACCESS_TOKEN:
        return jsonify({"status": "error", "message": "MP_ACCESS_TOKEN no configurado en el backend"}), 500

    producto = Producto.query.get(producto_id)
    if not producto:
        return jsonify({"status": "error", "message": "Producto no encontrado"}), 404

    # Generar un ID externo único para cada pago
    external_reference = f"pago-{producto.id}-{datetime.datetime.now().strftime('%Y%m%d%H%M%S%f')}"

    # Preferencia de pago para generar un QR de checkout (más genérico)
    preference_data = {
        "items": [
            {
                "title": f"{producto.nombre} ({producto.cantidad_ml}ml)",
                "quantity": 1,
                "unit_price": float(producto.precio) # Convertir Decimal a float para Mercado Pago
            }
        ],
        "external_reference": external_reference,
        "notification_url": f"{request.url_root}webhook_mp", # URL donde Mercado Pago enviará las notificaciones
        "back_urls": {
            "success": FRONTEND_URL, # Redirigir al frontend después del pago
            "pending": FRONTEND_URL,
            "failure": FRONTEND_URL
        },
        "auto_return": "approved" # Retornar automáticamente al frontend si el pago es aprobado
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
        mp_response.raise_for_status() # Lanza una excepción para códigos de estado HTTP de error (4xx o 5xx)
        preference = mp_response.json()
        
        # La URL del QR es 'init_point' para el checkout preference
        qr_url = preference.get('init_point') 
        if qr_url:
            return jsonify({
                "status": "success",
                "message": "QR generado exitosamente",
                "qr_data": qr_url, # Esta es la URL que el frontend usará para generar el QR
                "id_pago_mp": preference.get('id') # ID de la preferencia de MP
            })
        else:
            return jsonify({"status": "error", "message": "No se obtuvo 'init_point' de Mercado Pago"}), 500

    except requests.exceptions.RequestException as e:
        print(f"Error al conectar con la API de Mercado Pago: {e}")
        return jsonify({"status": "error", "message": f"Error de conexión con Mercado Pago: {e}"}), 500
    except Exception as e:
        print(f"Error inesperado al generar QR: {e}")
        return jsonify({"status": "error", "message": "Error inesperado al generar QR"}), 500

# Webhook de Mercado Pago
@app.route('/webhook_mp', methods=['POST'])
def webhook_mp():
    # Mercado Pago envía notificaciones a esta URL
    # Debes validar la autenticidad de la notificación si fuera un sistema de producción
    data = request.get_json()
    print(f"[WEBHOOK MP] Datos recibidos: {json.dumps(data, indent=2)}")

    # Mercado Pago primero envía una notificación de "merchant_order"
    # y luego notificaciones de "payment". Queremos procesar "payment".
    if data.get('type') == 'payment':
        payment_id = data.get('data', {}).get('id')
        if not payment_id:
            print("[WEBHOOK MP] ID de pago no encontrado en la notificación.")
            return jsonify({"status": "error", "message": "ID de pago no encontrado"}), 400

        headers = {
            "Authorization": f"Bearer {MP_ACCESS_TOKEN}"
        }
        try:
            # Obtener detalles completos del pago desde la API de MP
            payment_response = requests.get(
                f"https://api.mercadopago.com/v1/payments/{payment_id}",
                headers=headers
            )
            payment_response.raise_for_status()
            payment_info = payment_response.json()
            
            status = payment_info.get('status')
            external_reference = payment_info.get('external_reference')
            
            print(f"[WEBHOOK MP] Detalles del pago {payment_id}: Estado={status}, Ref. Externa={external_reference}")

            with app.app_context(): # Necesario para interactuar con la DB
                pago_existente = Pago.query.filter_by(id_pago_mp=str(payment_id)).first()
                
                if not pago_existente:
                    # Crear un nuevo registro de pago si no existe
                    # Extraer producto_id de external_reference (ej. "pago-1-20230727...")
                    try:
                        p_id_str = external_reference.split('-')[1] # asume formato "pago-PRODUCT_ID-TIMESTAMP"
                        producto_referenciado = Producto.query.get(int(p_id_str))
                    except (AttributeError, IndexError, ValueError):
                        producto_referenciado = None
                        print(f"No se pudo extraer producto_id de external_reference: {external_reference}")

                    nuevo_pago = Pago(
                        id_pago_mp=str(payment_id),
                        estado=status,
                        timestamp=datetime.datetime.now(),
                        dispensado=False,
                        producto=producto_referenciado # Asigna el objeto Producto
                    )
                    db.session.add(nuevo_pago)
                    db.session.commit()
                    print(f"Nuevo pago {payment_id} registrado con estado {status}.")
                else:
                    # Actualizar estado de un pago existente
                    if pago_existente.estado != status: # Solo actualiza si el estado cambió
                        pago_existente.estado = status
                        db.session.commit()
                        print(f"Estado de pago {payment_id} actualizado a {status}.")
                    else:
                        print(f"Pago {payment_id} ya registrado con estado {status}. No se requiere actualización.")

                # Si el pago está aprobado y no ha sido dispensado, enviar comando MQTT
                if status == "approved" and (not pago_existente or not pago_existente.dispensado):
                    print(f"Pago {payment_id} aprobado y no dispensado. Enviando comando MQTT.")
                    # Asegúrate de enviar el ID de pago a tu ESP32 para que lo use al reportar el estado
                    # También puedes enviar el ID del producto si es relevante para el ESP32
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

    return jsonify({"status": "ok"}), 200 # Siempre retornar 200 OK a Mercado Pago rápidamente

# Para ejecutar el servidor Flask
if __name__ == '__main__':
    # Esto solo se ejecuta si corres app.py directamente (no con gunicorn)
    # Es para desarrollo local
    with app.app_context():
        db.create_all() # Asegúrate de que las tablas se creen también en desarrollo local
    app.run(host='0.0.0.0', port=os.environ.get('PORT', 5000), debug=True)
