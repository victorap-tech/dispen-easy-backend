from flask import Flask, request, jsonify
import sqlite3

app = Flask(__name__)

# --- Ruta raíz de prueba ---
@app.route('/')
def home():
    return 'Backend de Dispen-Easy funcionando correctamente'

# --- Ruta para verificar estado de un pago ---
@app.route('/check_payment', methods=['GET'])
def check_payment():
    id_pago = request.args.get('id_pago')

    # Buscar el id_pago en la base de datos
    conn = sqlite3.connect('pagos.db')
    cursor = conn.cursor()
    cursor.execute("SELECT estado FROM pagos WHERE id_pago = ?", (id_pago,))
    resultado = cursor.fetchone()
    conn.close()

    if resultado and resultado[0] == "aprobado":
        return jsonify({"estado": "aprobado", "status": "ok"})
    else:
        return jsonify({"message": "pago no encontrado", "status": "error"})

# --- Webhook que recibe notificaciones POST de MercadoPago ---
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json()
    print("Webhook recibido:", data)

    if data and 'data' in data and 'id' in data['data']:
        id_pago = str(data['data']['id'])

        # Guardar en la base de datos como "aprobado"
        conn = sqlite3.connect('pagos.db')
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO pagos (id_pago, estado) VALUES (?, ?)", (id_pago, 'aprobado'))
        conn.commit()
        conn.close()

        return jsonify({"status": "ok", "id_pago": id_pago}), 200

    return jsonify({"status": "error", "message": "formato inválido"}), 400

# --- Iniciar servidor ---
if __name__ == '__main__':
    import os
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
