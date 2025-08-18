# models.py
from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime, Numeric, JSON, text
)
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class Producto(Base):
    __tablename__ = "producto"

    id         = Column(Integer, primary_key=True)
    slot_id    = Column(Integer, nullable=False, index=True, default=0)
    nombre     = Column(String(120), nullable=False, default="")
    precio     = Column(Numeric(10, 2), nullable=False, default=0)   # en ARS
    cantidad   = Column(Integer, nullable=False, default=1)          # litros
    habilitado = Column(Boolean, nullable=False, default=False)

    created_at = Column(DateTime, server_default=text("CURRENT_TIMESTAMP"))
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "slot_id": self.slot_id,
            "nombre": self.nombre,
            "precio": float(self.precio) if self.precio is not None else 0.0,
            "cantidad": self.cantidad,
            "habilitado": self.habilitado,
        }


class Pago(Base):
    __tablename__ = "pago"

    id         = Column(Integer, primary_key=True)
    id_pago    = Column(String(40), unique=True, index=True)         # payment_id / merchant_order id
    estado     = Column(String(32), nullable=False, default="pendiente")
    producto   = Column(String(120))                                  # nombre del producto
    slot_id    = Column(Integer)                                      # slot del producto
    monto      = Column(Numeric(10, 2))                               # ARS
    raw        = Column(JSON)                                         # payload crudo de MP
    dispensado = Column(Boolean, nullable=False, default=False)

    created_at = Column(DateTime, server_default=text("CURRENT_TIMESTAMP"))

    def to_dict(self):
        return {
            "id": self.id,
            "id_pago": self.id_pago,
            "estado": self.estado,
            "producto": self.producto,
            "slot_id": self.slot_id,
            "monto": float(self.monto) if self.monto is not None else None,
            "dispensado": self.dispensado,
        }
