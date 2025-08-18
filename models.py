# models.py
from datetime import datetime
from sqlalchemy import Column, Integer, String, Boolean, Text, DateTime
from database import Base

class Producto(Base):
    __tablename__ = "producto"

    id         = Column(Integer, primary_key=True, index=True)
    nombre     = Column(String(120), nullable=False)
    precio     = Column(Integer, nullable=False)        # en ARS, sin centavos
    cantidad   = Column(Integer, nullable=False, default=1)   # litros/unidades
    slot_id    = Column(Integer, nullable=False, index=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow,
                        onupdate=datetime.utcnow, nullable=False)

    def to_dict(self):
        return {
            "id": self.id,
            "slot_id": self.slot_id,
            "nombre": self.nombre,
            "precio": self.precio,
            "cantidad": self.cantidad,
        }

    def __repr__(self):
        return f"<Producto id={self.id} slot={self.slot_id} nombre={self.nombre!r}>"

class Pago(Base):
    __tablename__ = "pago"

    id         = Column(Integer, primary_key=True, index=True)
    id_pago    = Column(String(64), unique=True, nullable=False, index=True)  # MP payment id
    estado     = Column(String(32), nullable=False)                           # approved, pending, etc.
    producto   = Column(String(120))                                          # nombre del producto
    slot_id    = Column(Integer)                                              # slot asociado
    monto      = Column(Integer, nullable=False)                              # ARS enteros
    raw        = Column(Text)                                                 # JSON crudo del webhook
    dispensado = Column(Boolean, default=False, nullable=False)               # si ya se abrió el slot

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    def to_dict(self):
        return {
            "id": self.id,
            "id_pago": self.id_pago,
            "estado": self.estado,
            "producto": self.producto,
            "slot_id": self.slot_id,
            "monto": self.monto,
            "dispensado": self.dispensado,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self):
        return f"<Pago id_pago={self.id_pago} estado={self.estado}>"
