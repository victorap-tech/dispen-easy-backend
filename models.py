from sqlalchemy import Column, Integer, String
from database import Base

class Producto(Base):
    __tablename__ = 'producto'

    id = Column(Integer, primary_key=True, index=True)
    nombre = Column(String, nullable=False)
    precio = Column(Integer, nullable=False)
    cantidad = Column(Integer, nullable=False)
