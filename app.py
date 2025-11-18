import os
from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
import requests
from datetime import datetime

app = Flask(__name__)
CORS(app)

# ------------------------------
# CONFIG
# ------------------------------

DATABASE_URL = os.getenv("DATABASE_URL")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://")

app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

ADMIN_SECRET = os.getenv("ADMIN_SECRET", "adm123")
MP_ACCESS_TOKEN = os.getenv("MP_ACCESS_TOKEN", "")

# ------------------------------
# MODELOS
# ------------------------------

class Dispenser(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(50), unique=True, nullable=False)


class Producto(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    dispenser_id = db.Column(db.Integer, db.ForeignKey('dispenser.id'), nullable=False)
    slot = db.Column(db.Integer, nullable=False)   # 1 o 2
    nombre = db.Column(db.String(120), default="")
    precio = db.Column(db.Integer, default=0)

    dispenser = db.relationship("Dispenser", backref="productos")


class Pago(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    mp_payment_id = db.Column(db.String(80))
    estado = db.Column(db.String(40))
    monto = db.Column(db.Integer)
    slot = db.Column(db.Integer)
    producto_nombre = db.Column(db.String(120))
    dispenser_code = db.Column(db.String(50))
    fecha = db.Column(db.DateTime, default=datetime.utcnow)
