"""Conexión a SQL Server y utilidades de escritura."""
import re
from urllib.parse import quote_plus

from sqlalchemy import create_engine, text
from sqlalchemy.dialects.mssql import NVARCHAR, VARBINARY

from . import config


def motor(base=config.SQL_DB):
    partes = [f"DRIVER={{{config.SQL_DRIVER}}}", f"SERVER={config.SQL_SERVER}",
              f"DATABASE={base}", "TrustServerCertificate=yes"]
    if config.SQL_USER:
        partes += [f"UID={config.SQL_USER}", f"PWD={config.SQL_PASSWORD}"]
    else:
        partes.append("Trusted_Connection=yes")
    # fast_executemany: envía las filas por lotes en vez de una por una (inserciones masivas mucho más rápidas)
    return create_engine("mssql+pyodbc:///?odbc_connect=" + quote_plus(";".join(partes)),
                         fast_executemany=True)


def ejecutar_script(ruta, base="master"):
    """Ejecuta un .sql con lotes separados por GO (como sqlcmd/SSMS)."""
    sql = ruta.read_text(encoding="utf-8")
    lotes = [l for l in re.split(r"^\s*GO\s*$", sql, flags=re.M | re.I) if l.strip()]
    eng = motor(base).execution_options(isolation_level="AUTOCOMMIT")
    with eng.connect() as cn:
        for lote in lotes:
            cn.exec_driver_sql(lote)


def insertar(cn, df, tabla):
    """Inserta un DataFrame en una tabla existente (esquema.tabla) usando fast_executemany."""
    esquema, nombre = tabla.split(".")
    # Tipos explícitos: bytes -> VARBINARY (hashes) y texto -> NVARCHAR (tildes/ñ sin pérdida)
    tipos = {}
    for c in df.columns:
        muestra = df[c].dropna()
        if len(muestra) and isinstance(muestra.iloc[0], bytes):
            tipos[c] = VARBINARY(64)
        elif len(muestra) and isinstance(muestra.iloc[0], str):
            tipos[c] = NVARCHAR(4000)
    df.to_sql(nombre, cn, schema=esquema, if_exists="append", index=False, chunksize=5000, dtype=tipos)


def escalar(cn, sql, **params):
    return cn.execute(text(sql), params).scalar()
