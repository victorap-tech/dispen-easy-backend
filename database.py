import sqlite3

DB_NAME = "dispen_easy.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS pagos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            id_transaccion TEXT UNIQUE,
            monto REAL,
            producto TEXT,
            botella_reutilizable INTEGER,
            fecha TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

def guardar_pago(id_transaccion, monto, producto, botella_reutilizable):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''
        INSERT OR IGNORE INTO pagos (id_transaccion, monto, producto, botella_reutilizable)
        VALUES (?, ?, ?, ?)
    ''', (id_transaccion, monto, producto, botella_reutilizable))
    conn.commit()
    conn.close()

def pago_valido(id_transaccion):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('SELECT * FROM pagos WHERE id_transaccion = ?', (id_transaccion,))
    result = c.fetchone()
    conn.close()
    return result is not None