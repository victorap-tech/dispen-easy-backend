from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
import os
import datetime
import paho.mqtt.client as mqtt
import requests
import json
from decimal import Decimal

# --- Configuración de la Aplicación Flask ---
app = Flask(__name__, static_folder='static', template_folder='templates')
@app.route("/")
def index():
    return "✅ Backend Dispen-Easy funcionando"
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
FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:8000") # Asegúrate que coincida con el puerto de tu frontend

# --- Configuración MQTT ---
MQTT_BROKER = os.environ.get("MQTT_BROKER_HOST", "broker.hivemq.com") # Ej: xxxxx.s1.eu.hivemq.cloud
MQTT_PORT = int(os.environ.get("MQTT_BROKER_PORT", 1883)) # Ej: 8883 para SSL/TLS, 1883 para TCP
MQTT_USERNAME = os.environ.get("MQTT_BROKER_USERNAME", "")
MQTT_PASSWORD = os.environ.get("MQTT_BROKER_PASSWORD", "")

# Cliente MQTT
mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1) # Especifica la versión de la API
mqtt_client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print(f"Conectado exitosamente al broker MQTT: {MQTT_BROKER}")
        # Suscribirse a tópicos después de la conexión exitosa
        client.subscribe("dispensador/status") # Para que el ESP32 envíe el estado de dispensación
    else:
        print(f"Fallo la conexión al broker MQTT, código de retorno: {rc}")

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
    
