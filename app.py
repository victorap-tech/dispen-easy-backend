from flask import Flask, request, jsonify
import sqlite3

app = Flask(__name__)

# Crear la base de datos si no existe
conn = sqlite3.connect('pagos.db')
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

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json()
    id_pago = data.get('id_pago')
    estado = data.get('estado')

    conn = sqlite3.connect('pagos.db')
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO pagos (id_pago, estado, dispensado) VALUES (?, ?, 0)", (id_pago, estado))
    conn.commit()
    conn.close()

    return jsonify({'mensaje': 'Pago recibido'}), 200

@app.route('/check_payment', methods=['GET'])
def check_payment():
    id_pago = request.args.get('id_pago')

    conn = sqlite3.connect('pagos.db')
    cursor = conn.cursor()
    cursor.execute("SELECT estado, dispensado FROM pagos WHERE id_pago = ?", (id_pago,))
    row = cursor.fetchone()
    conn.close()

    if row:
        estado, dispensado = row
        return jsonify({'estado': estado, 'dispensado': dispensado})
    else:
        return jsonify({'error': 'Pago no encontrado'}), 404

@app.route('/marcar_dispensado', methods=['POST'])
def marcar_dispensado():
    data = request.get_json()
    id_pago = data.get('id_pago')

    conn = sqlite3.connect('pagos.db')
    cursor = conn.cursor()
    cursor.execute("UPDATE pagos SET dispensado = 1 WHERE id_pago = ?", (id_pago,))
    conn.commit()
    conn.close()

    return jsonify({'mensaje': 'Marcado como dispensado'}), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
   


