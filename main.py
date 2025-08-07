from fastapi import FastAPI, Request
from typing import Dict

app = FastAPI()

pagos = {}

@app.post("/webhook")
async def recibir_pago(request: Request):
    data = await request.json()
    print("Webhook recibido:", data)

    pago_id = str(data.get("data", {}).get("id", "sin_id"))
    estado = data.get("type", "desconocido")

    pagos[pago_id] = estado
    return {"status": "ok"}

@app.get("/verificar/{pago_id}")
def verificar_pago(pago_id: str):
    estado = pagos.get(pago_id, "no_encontrado")
    return {"estado": estado}