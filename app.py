from flask import Flask, request, jsonify

app = Flask(__name__)

# Endpoint de prueba
@app.route('/')
def home():
    return 'Backend de Dispen-Easy funcionando correctamente.'

# Endpoint para verificar pago
@app.route('/check_payment', methods=['GET'])
def check_payment():
    id_pago = request.args.get('id_pago')
    if id_pago == '312344':
        return jsonify({"estado": "aprobado", "status": "ok"})
    else:
        return jsonify({"message": "Pago no encontrado", "status": "error"})

import os

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environt.get('PORT',5000)))
