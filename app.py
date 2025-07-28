from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
import os
import datetime
import requests # Para hacer llamadas a la API de Mercado Pago
import paho.mqtt.client as mqtt # Cliente MQTT para el backend
import json # Para manejar los payloads MQTT

# --- Configuración de la Aplicación Flask ---
app = Flask(__name__, static_folder='static', template_folder='templates')

# --- CONFIGURACIÓN DE LA BASE DE DATOS ---
# Railway inyectará DATABASE_URL para PostgreSQL.
# Para desarrollo local, usará 'sqlite:///pagos.db'.
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///pagos.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False # Recomendado para evitar warnings

db = SQLAlchemy(app) # Inicializa la extensión SQLAlchemy

# --- CONFIGURACIÓN CORS ---
# Obtén el dominio de tu frontend desde una variable de entorno en Railway.
# En Railway, en tu servicio de backend, añade una variable:
# FRONTEND_URL = https://tu-frontend-nombre-random.railway.app
# Para desarrollo local, puedes usar "http://localhost:XXXX" donde XXXX es el puerto de tu frontend.
FRONTEND_URL = os.environ.get("FRONTEND_URL", "https://dispen-easy-web-production.up.railway.app/")
CORS(app, resources={r"/api/*": {"origins": FRONTEND_URL}})

# --- CONFIGURACIÓN DE MERCADO PAGO ---
# Tu Access Token de Mercado Pago (SANDBOX para pruebas, PRODUCCIÓN para despliegue real)
# Configura esto como una variable de entorno en Railway: MP_ACCESS_TOKEN
MP_ACCESS_TOKEN = os.environ.get("MP_ACCESS_TOKEN")
if not MP_ACCESS_TOKEN:
    print("ADVERTENCIA: MP_ACCESS_TOKEN no configurado. Las operaciones de Mercado Pago no funcionarán.")
    # raise ValueError("MP_ACCESS_TOKEN no configurado en las variables de entorno.") # Descomentar para producción

# --- CONFIGURACIÓN MQTT ---
# Configura estas variables de entorno en Railway para tu broker MQTT (ej. HiveMQ Cloud)
MQTT_BROKER = os.environ.get("MQTT_BROKER_HOST", "c9b4a2b821ec4e87b10ed8e0ace8e4ee.s1.eu.hivemq.cloud") # Ej: xxxxx.s1.eu.hivemq.cloud
MQTT_PORT = int(os.environ.get("MQTT_BROKER_PORT", 8883)) # Ej: 8883 para SSL/TLS
MQTT_USERNAME = os.environ.get("MQTT_BROKER_USERNAME", "Victor")
MQTT_PASSWORD = os.environ.get("MQTT_BROKER_PASSWORD", "Dispen-easy25")

MQTT_TOPIC_COMANDO = "dispensador/comando" # Para enviar comandos al ESP
MQTT_TOPIC_STATUS_ESP = "dispensador/status" # Para recibir estados del ESP

# --- CLIENTE MQTT ---
mqtt_client = mqtt.Client(client_id="backend_dispensador")

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print(f"Conectado exitosamente al broker MQTT: {MQTT_BROKER}")
        mqtt_client.subscribe(MQTT_TOPIC_STATUS_ESP) # El backend se suscribe a los estados del ESP
        print(f"Suscrito al tópico: {MQTT_TOPIC_STATUS_ESP}")
    else:
        print(f"Fallo al conectar al broker MQTT, código de retorno: {rc}")

def on_message(client, userdata, msg):
    # Callback para procesar mensajes MQTT recibidos del ESP
    topic = msg.topic
    payload = msg.payload.decode()
    print(f"Mensaje MQTT recibido de ESP en '{topic}': {payload}")

    if topic == MQTT_TOPIC_STATUS_ESP:
        try:
            data = json.loads(payload)
            transaction_id = data.get("transaction_id")
            status_esp = data.get("status")
            volumen_dispensado_ml = data.get("volumen_dispensado_ml", 0.0)
            tiempo_real_segundos = data.get("tiempo_real_segundos", 0.0) # Para pruebas basadas en tiempo

            if transaction_id:
                with app.app_context(): # Es crucial operar la DB dentro del contexto de la app
                    pago_a_actualizar = Pago.query.get(transaction_id)
                    if pago_a_actualizar:
                        pago_a_actualizar.estado = status_esp # Actualiza el estado directamente
                        pago_a_actualizar.cantidad_ml_dispensado = volumen_dispensado_ml
                        db.session.commit()
                        print(f"Pago {transaction_id} actualizado a '{status_esp}' en la BD.")
                    else:
                        print(f"Transacción {transaction_id} no encontrada en la BD para actualizar.")
            else:
                print("Mensaje de estado del ESP sin transaction_id.")
        except Exception as e:
            print(f"Error procesando mensaje de estado del ESP: {e}")

def connect_mqtt():
    mqtt_client.on_connect = on_connect
    mqtt_client.on_message = on_message
    if MQTT_USERNAME and MQTT_PASSWORD:
        mqtt_client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
    try:
        mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
        mqtt_client.loop_start() # Inicia un hilo en segundo plano para manejar la red MQTT
    except Exception as e:
        print(f"No se pudo conectar al broker MQTT: {e}")

# --- MODELOS DE LA BASE DE DATOS ---
class Producto(db.Model):
    __tablename__ = 'productos'
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(100), nullable=False, unique=True)
    precio = db.Column(db.Float, nullable=False)
    cantidad_ml = db.Column(db.Float, nullable=False) # Cantidad en ml que dispensa este producto
    mp_external_reference = db.Column(db.String(255), unique=True, nullable=True) # Para vincular con MP QR
    gabinete_id = db.Column(db.String(50), nullable=False) # Para identificar a qué dispensador pertenece
    # Opcional: para la dispensación por tiempo, podrías añadir un campo
    # tiempo_dispensacion_segundos = db.Column(db.Integer, nullable=True)

    def __repr__(self):
        return f'<Producto {self.nombre} ({self.cantidad_ml}ml)>'

    def to_dict(self):
        return {
            'id': self.id,
            'nombre': self.nombre,
            'precio': self.precio,
            'cantidad_ml': self.cantidad_ml,
            'mp_external_reference': self.mp_external_reference,
            'gabinete_id': self.gabinete_id
        }

class Pago(db.Model):
    __tablename__ = 'pagos'
    id = db.Column(db.Integer, primary_key=True)
    mp_payment_id = db.Column(db.String(100), unique=True, nullable=False) # ID del pago en Mercado Pago
    external_reference = db.Column(db.String(255), nullable=True) # Tu referencia externa de MP
    producto_id = db.Column(db.Integer, db.ForeignKey('productos.id'), nullable=False)
    producto = db.relationship('Producto', backref=db.backref('pagos', lazy=True))
    cantidad_ml_objetivo = db.Column(db.Float, nullable=False) # La cantidad que se pidió dispensar
    cantidad_ml_dispensado = db.Column(db.Float, default=0.0) # Lo que realmente se dispensó (del sensor de caudal)
    estado = db.Column(db.String(50), nullable=False) # Ej: 'pendiente', 'aprobado', 'rechazado', 'dispensando', 'dispensado', 'error_dispensacion'
    fecha_creacion = db.Column(db.DateTime, default=datetime.datetime.now)
    fecha_actualizacion = db.Column(db.DateTime, default=datetime.datetime.now, onupdate=datetime.datetime.now)
    gabinete_id = db.Column(db.String(50), nullable=False)

    def __repr__(self):
        return f'<Pago {self.mp_payment_id} - {self.estado}>'

    def to_dict(self):
        return {
            'id': self.id,
            'mp_payment_id': self.mp_payment_id,
            'external_reference': self.external_reference,
            'producto_id': self.producto_id,
            'cantidad_ml_objetivo': self.cantidad_ml_objetivo,
            'cantidad_ml_dispensado': self.cantidad_ml_dispensado,
            'estado': self.estado,
            'fecha_creacion': self.fecha_creacion.isoformat(),
            'fecha_actualizacion': self.fecha_actualizacion.isoformat(),
            'gabinete_id': self.gabinete_id
        }

# --- FUNCIÓN PARA CREAR TABLAS ---
def create_tables():
    with app.app_context():
        db.create_all()
        print("Tablas de la base de datos creadas/verificadas.")

# --- RUTAS DE LA API (PARA EL FRONTEND DE ADMINISTRACIÓN) ---

# Ruta para servir el index.html principal del frontend de administración
@app.route('/')
def index():
    return render_template('index.html')

# Obtener todos los productos
@app.route('/api/productos', methods=['GET'])
def get_productos():
    productos = Producto.query.all()
    return jsonify([p.to_dict() for p in productos])

# Crear un nuevo producto
@app.route('/api/productos', methods=['POST'])
def create_producto():
    data = request.json
    # Asegúrate de que estos campos existan en el payload del frontend
    if not all(k in data for k in ['nombre', 'precio', 'cantidad_ml', 'gabinete_id']):
        return jsonify({"error": "Faltan datos requeridos (nombre, precio, cantidad_ml, gabinete_id)"}), 400

    # Generar un external_reference único para Mercado Pago
    # Usaremos una combinación del nombre del producto y un timestamp
    mp_external_ref = f"{data['nombre'].replace(' ', '_').lower()}_{int(datetime.datetime.now().timestamp())}"

    nuevo_producto = Producto(
        nombre=data['nombre'],
        precio=data['precio'],
        cantidad_ml=data['cantidad_ml'],
        mp_external_reference=mp_external_ref,
        gabinete_id=data['gabinete_id']
    )
    db.session.add(nuevo_producto)
    db.session.commit()

    return jsonify(nuevo_producto.to_dict()), 201

# Obtener un producto por ID
@app.route('/api/productos/<int:producto_id>', methods=['GET'])
def get_producto(producto_id):
    producto = Producto.query.get_or_404(producto_id)
    return jsonify(producto.to_dict())

# Actualizar un producto existente
@app.route('/api/productos/<int:producto_id>', methods=['PUT'])
def update_producto(producto_id):
    producto = Producto.query.get_or_404(producto_id)
    data = request.json
    producto.nombre = data.get('nombre', producto.nombre)
    producto.precio = data.get('precio', producto.precio)
    producto.cantidad_ml = data.get('cantidad_ml', producto.cantidad_ml)
    producto.gabinete_id = data.get('gabinete_id', producto.gabinete_id)
    # mp_external_reference no debería cambiarse fácilmente una vez generado
    db.session.commit()
    return jsonify(producto.to_dict())

# Eliminar un producto
@app.route('/api/productos/<int:producto_id>', methods=['DELETE'])
def delete_producto(producto_id):
    producto = Producto.query.get_or_404(producto_id)
    db.session.delete(producto)
    db.session.commit()
    return jsonify({"message": "Producto eliminado"}), 204

# --- API para generar QR de Mercado Pago ---
@app.route('/api/generar_qr/<int:producto_id>', methods=['POST'])
def generar_qr_mercadopago(producto_id):
    if not MP_ACCESS_TOKEN:
        return jsonify({"error": "MP_ACCESS_TOKEN no configurado en el servidor."}), 500

    producto = Producto.query.get(producto_id)
    if not producto:
        return jsonify({"error": "Producto no encontrado"}), 404

    # Crear la preferencia de pago para el QR
    preference_data = {
        "external_reference": producto.mp_external_reference,
        "items": [
            {
                "title": producto.nombre,
                "quantity": 1,
                "unit_price": producto.precio,
                "currency_id": "ARS" # O la moneda de tu país
            }
        ],
        "notification_url": f"{request.url_root.rstrip('/')}/webhook-mercadopago"
        # request.url_root obtendrá la URL base de tu backend en Railway
        # Asegúrate de que esta URL sea accesible públicamente por Mercado Pago
    }

    # URL de la API de creación de QR
    mp_qr_api_url = "https://api.mercadopago.com/instore/qr/seller/collectors/YOUR_COLLECTOR_ID/pos/YOUR_POS_ID/qrs"
    # IMPORTANTE: Reemplaza YOUR_COLLECTOR_ID y YOUR_POS_ID.
    # El collector_id es tu user_id de MP (lo encuentras en tus credenciales de desarrollador).
    # El pos_id es un ID inventado por ti, por ejemplo "dispen_easy_pos_01".
    # Puedes generar estos en tu cuenta de Mercado Pago Developers o usar IDs de prueba.
    # Para simplificar, puedes usar la API de 'Point' para generar un QR de un link de pago:
    # Esta es una forma más sencilla y común para QRs estáticos
    mp_qr_api_url = "https://api.mercadopago.com/checkout/preferences"


    headers = {
        "Authorization": f"Bearer {MP_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }

    try:
        response = requests.post(mp_qr_api_url, headers=headers, json=preference_data)
        response.raise_for_status() # Lanza una excepción para errores HTTP (4xx o 5xx)
        mp_response = response.json()

        qr_data = None
        if 'qr_data' in mp_response: # Si la respuesta es de la API de QR directa
            qr_data = mp_response['qr_data']
        elif 'init_point' in mp_response: # Si la respuesta es de la API de Checkout Preferences
            # Mercado Pago devuelve 'init_point' que es una URL. Puedes generar un QR a partir de esta URL.
            qr_data = mp_response['init_point']
        elif 'point_of_interaction' in mp_response and 'qr_base64' in mp_response['point_of_interaction']:
             # Si usas la API de 'Point', puede devolver qr_base64 directamente
             qr_data = mp_response['point_of_interaction']['qr_base64']

        if qr_data:
            return jsonify({"status": "success", "qr_data": qr_data, "external_reference": producto.mp_external_reference}), 200
        else:
            return jsonify({"error": "No se pudo obtener el QR data de Mercado Pago", "mp_response": mp_response}), 500

    except requests.exceptions.RequestException as e:
        print(f"Error al llamar a la API de Mercado Pago: {e}")
        return jsonify({"error": "Error de conexión con Mercado Pago", "details": str(e)}), 500
    except Exception as e:
        print(f"Error inesperado al generar QR: {e}")
        return jsonify({"error": "Error interno al generar QR", "details": str(e)}), 500

# --- WEBHOOK DE MERCADO PAGO ---
@app.route('/webhook-mercadopago', methods=['POST'])
def mercadopago_webhook():
    if not MP_ACCESS_TOKEN:
        print("Error: MP_ACCESS_TOKEN no configurado para el webhook.")
        return jsonify({"status": "error", "message": "Backend no configurado para MP"}), 500

    data = request.json
    print(f"[WEBHOOK MP] Datos recibidos: {data}")

    # Es crucial verificar el tipo de notificación y el ID del recurso
    topic = data.get("type") # 'payment', 'merchant_order', etc.
    resource_id = data.get("id") # ID del recurso (ej. ID del pago)

    if not topic or not resource_id:
        print("Webhook de MP inválido: falta 'type' o 'id'.")
        return jsonify({"status": "error", "message": "Invalid webhook payload"}), 400

    if topic == "payment":
        try:
            # Consultar la API de Mercado Pago para obtener los detalles completos del pago
            payment_details_url = f"https://api.mercadopago.com/v1/payments/{resource_id}"
            headers = {"Authorization": f"Bearer {MP_ACCESS_TOKEN}"}
            response = requests.get(payment_details_url, headers=headers)
            response.raise_for_status()
            payment_data = response.json()

            payment_mp_id = payment_data.get("id")
            status_mp = payment_data.get("status")
            external_reference_mp = payment_data.get("external_reference") # Tu ID de producto/transacción

            print(f"Detalles de pago MP - ID: {payment_mp_id}, Estado: {status_mp}, Ref Externa: {external_reference_mp}")

            with app.app_context():
                producto_asociado = Producto.query.filter_by(mp_external_reference=external_reference_mp).first()

                if producto_asociado:
                    # Busca si ya existe un registro de este pago
                    pago_existente = Pago.query.filter_by(mp_payment_id=payment_mp_id).first()

                    if not pago_existente:
                        # Si es un pago nuevo
                        nuevo_pago_registro = Pago(
                            mp_payment_id=payment_mp_id,
                            external_reference=external_reference_mp,
                            producto_id=producto_asociado.id,
                            cantidad_ml_objetivo=producto_asociado.cantidad_ml,
                            estado=status_mp, # Guarda el estado de MP
                            gabinete_id=producto_asociado.gabinete_id
                        )
                        db.session.add(nuevo_pago_registro)
                        db.session.commit()
                        print(f"Pago {payment_mp_id} registrado como '{status_mp}' para producto '{producto_asociado.nombre}'.")

                        # --- LÓGICA DE DISPENSACIÓN (ENVÍO MQTT) ---
                        if status_mp == "approved":
                            # Aquí se envía el comando al ESP vía MQTT
                            # Para las pruebas por tiempo, usamos un tiempo fijo o asociado al producto
                            tiempo_a_dispensar_segundos = 10 # TIEMPO FIJO DE PRUEBA
                            # O podrías tener un campo 'tiempo_dispensacion_segundos' en tu modelo Producto
                            # tiempo_a_dispensar_segundos = producto_asociado.tiempo_dispensacion_segundos

                            mensaje_mqtt = {
                                "comando": "dispensar",
                                "transaction_id": nuevo_pago_registro.id, # Usamos el ID de nuestro registro de Pago
                                "producto": producto_asociado.nombre,
                                "tiempo_segundos": tiempo_a_dispensar_segundos, # Enviamos el tiempo en segundos
                                "gabinete_id": producto_asociado.gabinete_id
                            }
                            mqtt_client.publish(MQTT_TOPIC_COMANDO, json.dumps(mensaje_mqtt), qos=1)
                            print(f"Comando MQTT de dispensación enviado: {mensaje_mqtt}")
                            # Actualizar el estado del pago a 'en_proceso_dispensacion' o similar
                            nuevo_pago_registro.estado = "en_proceso_dispensacion"
                            db.session.commit()

                    elif pago_existente:
                        # Si el pago ya existe, actualiza su estado si ha cambiado
                        if pago_existente.estado != status_mp:
                            pago_existente.estado = status_mp
                            db.session.commit()
                            print(f"Estado de pago {payment_mp_id} actualizado a '{status_mp}'.")
                        else:
                            print(f"Pago {payment_mp_id} ya tenía estado '{status_mp}'. No se actualiza.")

                else:
                    print(f"External reference '{external_reference_mp}' del pago no asociada a ningún producto conocido.")

        except requests.exceptions.RequestException as e:
            print(f"Error al consultar detalles de pago de MP: {e}")
            return jsonify({"status": "error", "message": "Error al consultar MP"}), 500
        except Exception as e:
            print(f"Error procesando webhook de MP: {e}")
            return jsonify({"status": "error", "message": str(e)}), 500
    else:
        print(f"Webhook recibido para tópico no manejado: {topic}")

    return jsonify({"status": "success"}), 200 # Mercado Pago espera un 200 OK

# --- Bucle Principal ---
if __name__ == '__main__':
    create_tables() # Crea las tablas de la DB (PostgreSQL en Railway, SQLite local)
    connect_mqtt()  # Conecta el cliente MQTT

    # Para ejecución en Railway, usa la variable de entorno PORT.
    # Para desarrollo local, usa 5000.
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=True, host='0.0.0.0', port=port)
