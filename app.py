from flask import Flask, request, render_template_string

app = Flask(__name__)

# Precios por volumen por producto
PRECIOS = {
    'lavandina': {1: 500, 2: 900},
    'detergente': {1: 600, 2: 1000},
    'jabon': {1: 550, 2: 950}
}

@app.route('/pago')
def pagina_pago():
    producto = request.args.get('producto')
    if producto not in PRECIOS:
        return "Producto no válido", 400

    precio_1l = PRECIOS[producto][1]
    precio_2l = PRECIOS[producto][2]

    html = f"""
    <html>
        <head>
            <title>Dispen-easy - Pago</title>
            <style>
                body {{
                    font-family: sans-serif;
                    text-align: center;
                    padding: 30px;
                    background-color: #f0f8ff;
                }}
                .logo {{
                    width: 120px;
                    margin-bottom: 20px;
                }}
                h2 {{
                    color: #007bff;
                    font-size: 32px;
                }}
                button {{
                    font-size: 24px;
                    padding: 20px 40px;
                    margin: 20px;
                    background-color: #007bff;
                    color: white;
                    border: none;
                    border-radius: 12px;
                    cursor: pointer;
                    width: 80%;
                    max-width: 400px;
                }}
                button:hover {{
                    background-color: #0056b3;
                }}
            </style>
        </head>
        <body>
            <img src="https://i.imgur.com/Nr9V0QX.png" alt="Logo Dispen-easy" class="logo">
            <h2>Dispen-easy</h2>
            <p><strong>{producto.capitalize()}</strong></p>
            <form action="/pagar" method="get">
                <input type="hidden" name="producto" value="{producto}">
                <input type="hidden" name="volumen" value="1">
                <button type="submit">💧 Comprar 1 litro - ${precio_1l}</button>
            </form>
            <form action="/pagar" method="get">
                <input type="hidden" name="producto" value="{producto}">
                <input type="hidden" name="volumen" value="2">
                <button type="submit">💦 Comprar 2 litros - ${precio_2l}</button>
            </form>
        </body>
    </html>
    """
    return render_template_string(html)

@app.route('/pagar')
def simular_pago():
    producto = request.args.get('producto')
    volumen = request.args.get('volumen')
    return f"💳 Simulando pago de {volumen}L de {producto}. (Acá iría el link de MercadoPago)"

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)