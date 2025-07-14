from flask import Flask, request, jsonify
import sqlite3
import os

app = Flask(__name__)

# Inicializa la base de datos
def init_db():
    conn = sqlite3.connect('pagos.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS pagos (
            id_pago TEXT PRIMARY KEY,
            estado TEXT
        )
    ''')
    conn.commit()
    conn.close()

# Endpoint para recibir pagos (webhook de MercadoPago)
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json()
    if not data:
        return 'Invalid payload', 400

    # Extrae el ID de pago y estado
    id_pago = str(data.get('id_pago'))
    estado = data.get('estado', 'pendiente')

    if id_pago:
        conn = sqlite3.connect('pagos.db')
        cursor = conn.cursor()
        cursor.execute('INSERT OR REPLACE INTO pagos (id_pago, estado) VALUES (?, ?)', (id_pago, estado))
        conn.commit()
        conn.close()
        return 'Pago guardado', 200
    else:
        return 'ID de pago no encontrado', 400

# Endpoint para consultar el estado de un pago
@app.route('/check_payment', methods=['GET'])
def check_payment():
    id_pago = request.args.get('id_pago')
    if not id_pago:
        return jsonify({'error': 'Falta el parámetro id_pago'}), 400

    conn = sqlite3.connect('pagos.db')
    cursor = conn.cursor()
    cursor.execute('SELECT estado FROM pagos WHERE id_pago = ?', (id_pago,))
    row = cursor.fetchone()
    conn.close()

    if row:
        return jsonify({'estado': row[0]})
    else:
        return jsonify({'estado': 'no_encontrado'})

# Inicializa la base de datos al iniciar
init_db()

# Puerto dinámico para Railway
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)

     
  
