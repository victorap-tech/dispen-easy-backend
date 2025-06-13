from flask import Flask, request, jsonify 
import sqlite3 
import os

app = Flask(name)

#Inicializar base de datos

def init_db(): 
    conn = sqlite3.connect('pagos.db') 
    cursor = conn.cursor() 
cursor.execute('''CREATE TABLE IF NOT EXISTS pagos ( id_pago TEXT PRIMARY KEY, status TEXT )''') 
conn.commit() 
conn.close()

init_db()

#Ruta de prueba

@app.route('/') def home(): return 'Backend de Dispen-Easy funcionando correctamente'

#Webhook de MercadoPago

@app.route('/webhook', methods=['POST']) 
def webhook(): 
    data = request.get_json() 
    if not data: 
        return jsonify({'status': 'error', 'message': 'No JSON received'}), 400

payment_id = str(data.get('data', {}).get('id'))
if payment_id:
    conn = sqlite3.connect('pagos.db')
    cursor = conn.cursor()
    cursor.execute('INSERT OR REPLACE INTO pagos (id_pago, status) VALUES (?, ?)', (payment_id, 'aprobado'))
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok', 'message': 'Pago recibido'}), 200
else:
    return jsonify({'status': 'error', 'message': 'ID de pago no encontrado'}), 400

#Verificar pago desde ESP32

@app.route('/check_payment', methods=['GET']) 
def check_payment(): 
    id_pago = request.args.get('id_pago') 
  if not id_pago: 
      return jsonify({'status': 'error', 'message': 'Falta id_pago'}), 400

conn = sqlite3.connect('pagos.db')
cursor = conn.cursor()
cursor.execute('SELECT status FROM pagos WHERE id_pago = ?', (id_pago,))
result = cursor.fetchone()
conn.close()

if result and result[0] == 'aprobado':
    return jsonify({'estado': 'aprobado', 'status': 'ok'})
else:
    return jsonify({'message': 'pago no encontrado', 'status': 'error'})

if name == 'main': 
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
