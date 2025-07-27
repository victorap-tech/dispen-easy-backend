from flask import Flask, request
import sqlite3

app = Flask(__name__)

@app.route('/check_payment')
def check_payment():
    id_pago = request.args.get('id_pago')
    conn = sqlite3.connect('pagos.db')
    cursor = conn.cursor()
    cursor.execute("SELECT estado FROM pagos WHERE id_pago=?", (id_pago,))
    result = cursor.fetchone()
    conn.close()
    if result:
        return result[0]  # Devuelve 'aprobado' o lo que haya
    else:
        return "no_encontrado"

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)