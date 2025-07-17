from flask import Flask, request, jsonify
import sqlite3
import os

app = Flask(__name__)
DB_PATH = "pagos.db"

# Inicializar la base de datos
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS pagos (
            id_pago TEXT PRIMARY KEY,
            estado TEXT,
            dispensado INTEGER DEFAULT 0
        )
    ''')
    conn.commit()
    conn.close()

init_db()

@app.route('/')
def index():
    return "Servidor Flask de Dispen-Easy funcionando."

# Webhook que recibe pagos
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json()
    id_pago = data.get("id_pago")
    estado = data.get("estado")

    if id_pago and estado:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO pagos (id_pago, estado, dispensado) VALUES (?, ?, 0)", (id_pago, estado))
        conn.commit()
        conn.close()
        return jsonify({"mensaje": "Pago recibido"}), 200
    else:
        return jsonify({"error": "Datos incompletos"}), 400

# Endpoint para el ESP32: consulta pagos aprobados pendientes
@app.route('/check_payment_pendiente', methods=['GET'])
def check_payment_pendiente():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id_pago FROM pagos WHERE estado = 'aprobado' AND dispensado = 0 LIMIT 1")
    fila = cursor.fetchone()
    conn.close()

    if fila:
        return jsonify({"id_pago": fila[0], "estado": "aprobado"})
    else:
        return jsonify({"mensaje": "No hay pagos pendientes."})

# Endpoint para marcar un pago como dispensado
@app.route('/marcar_dispensado', methods=['POST'])
def marcar_dispensado():
    data = request.get_json()
    id_pago = data.get("id_pago")

    if id_pago:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("UPDATE pagos SET dispensado = 1 WHERE id_pago = ?", (id_pago,))
        conn.commit()
        conn.close()
        return jsonify({"mensaje": "Pago marcado como dispensado"}), 200
    else:
        return jsonify({"error": "ID de pago no proporcionado"}), 400

# Lanzar la app en el puerto que Railway define
#if __name__ == '__main__':
    #port = int(os.environ.get('PORT', 5000))  # Para producción
    #app.run(host='0.0.0.0', port=port)
   
