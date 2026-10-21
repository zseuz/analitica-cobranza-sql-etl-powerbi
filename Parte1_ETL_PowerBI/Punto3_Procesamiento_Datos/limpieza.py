"""Funciones de limpieza y reglas de negocio compartidas por todos los procesos.

Hallazgos de calidad que motivan cada regla (perfilamiento de las fuentes):
  * ID de crédito con prefijos/sufijos de texto en ~25 % de las filas de las bases 2-4
    ('OP-1685153-A', 'C_1135300_OK', 'CTO1323881X', '1192400B' ...). Todas las variantes
    contienen un único bloque numérico -> ese número es la llave del crédito.
  * Textos categóricos con mayúsculas, tildes, espacios y símbolos sueltos
    ('Sin-Contacto.', ' #Sin Contacto ', 'banco bogota_', 'Normal*' ...) -> se normalizan
    a una llave ASCII en minúsculas y se mapean a un catálogo cerrado.
  * Fechas como enteros YYYYMMDD y horas como texto HH:MM:SS.
"""
import hashlib
import re
import unicodedata

import numpy as np
import pandas as pd


# --------------------------------------------------------------------------- texto
def llave_texto(valor):
    """'  #Sin-Contacto. ' -> 'sin contacto' (sin tildes, símbolos ni espacios dobles)."""
    if pd.isna(valor):
        return None
    s = unicodedata.normalize("NFKD", str(valor)).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip() or None


def mapear_catalogo(serie, catalogo):
    """Normaliza y traduce con un catálogo cerrado. Devuelve (serie_mapeada, valores_no_mapeados)."""
    llaves = serie.map(llave_texto)
    mapeada = llaves.map(catalogo)
    desconocidos = sorted(set(llaves[mapeada.isna() & llaves.notna()]))
    return mapeada, desconocidos


def normalizar_columnas(df):
    """'tipo de mensaje' -> 'tipo_mensaje'; 'Año+Mes' -> 'ano_mes'; descarta columnas 'Unnamed'."""
    df = df.loc[:, ~df.columns.astype(str).str.startswith("Unnamed")].copy()
    cols = [llave_texto(c).replace(" de ", " ").replace(" ", "_") for c in df.columns]
    df.columns = cols
    return df


# --------------------------------------------------------------------------- llaves
def credito_id(serie):
    """Extrae el bloque numérico del código de crédito. Devuelve Int64 (NA si no hay dígitos)."""
    return pd.to_numeric(serie.astype(str).str.extract(r"(\d+)", expand=False), errors="coerce").astype("Int64")


def celular_movil(serie):
    """Deja el celular como 10 dígitos colombianos (3XXXXXXXXX). Inválido -> NA.

    La fuente trae 12 dígitos: 2 dígitos adicionales al inicio y el móvil
    (p. ej. '873001234567' -> '3001234567'); se conservan los últimos 10 si empiezan por 3.
    """
    dig = serie.astype(str).str.replace(r"\D", "", regex=True)
    ult10 = dig.str[-10:]
    return ult10.where(ult10.str.fullmatch(r"3\d{9}"))


def fecha_desde_entero(serie):
    """20250620 -> Timestamp; vacíos o inválidos -> NaT."""
    return pd.to_datetime(pd.to_numeric(serie, errors="coerce").astype("Int64").astype(str),
                          format="%Y%m%d", errors="coerce")


def fecha_id(serie_fechas):
    return pd.to_numeric(serie_fechas.dt.strftime("%Y%m%d"), errors="coerce").astype("Int64")


def sha256(*columnas):
    """Hash binario fila a fila de varias columnas (llave natural estable)."""
    unidas = columnas[0].astype(str)
    for c in columnas[1:]:
        unidas = unidas + "|" + c.astype(str)
    return unidas.map(lambda s: hashlib.sha256(s.encode("utf-8")).digest())


def numero(serie):
    return pd.to_numeric(serie, errors="coerce")


# --------------------------------------------------------------------------- catálogos
# Tipificaciones de gestión: llave normalizada -> (nombre, canal, categoria,
#   es_intento_contacto, es_contacto, es_contacto_directo, es_compromiso)
TIPIFICACIONES = {
    "promesa pago":                    ("Promesa de pago", "Telefónica", "Contacto directo", 1, 1, 1, 1),
    "acuerdo de pago":                 ("Acuerdo de pago", "Telefónica", "Contacto directo", 1, 1, 1, 1),
    "posibilidad de pago":             ("Posibilidad de pago", "Telefónica", "Contacto directo", 1, 1, 1, 0),
    "negociacion":                     ("Negociación", "Telefónica", "Contacto directo", 1, 1, 1, 0),
    "alternativa normalizacion":       ("Alternativa de normalización", "Telefónica", "Contacto directo", 1, 1, 1, 0),
    "en tramite alternativa":          ("En trámite alternativa", "Telefónica", "Contacto directo", 1, 1, 1, 0),
    "pago parcial":                    ("Pago parcial", "Telefónica", "Contacto directo", 1, 1, 1, 0),
    "novedad pago":                    ("Novedad de pago", "Telefónica", "Contacto directo", 1, 1, 1, 0),
    "novedad":                         ("Novedad", "Telefónica", "Contacto directo", 1, 1, 1, 0),
    "renuente":                        ("Renuente", "Telefónica", "Contacto directo", 1, 1, 1, 0),
    "dacion adjudicacion":             ("Dación / adjudicación", "Telefónica", "Contacto directo", 1, 1, 1, 0),
    "cliente remitido oficina":        ("Cliente remitido a oficina", "Telefónica", "Contacto directo", 1, 1, 1, 0),
    "reclamacion seguros":             ("Reclamación seguros", "Telefónica", "Contacto directo", 1, 1, 1, 0),
    "reclamacion siniestro":           ("Reclamación siniestro", "Telefónica", "Contacto directo", 1, 1, 1, 0),
    "reporte siniestro vehiculo":      ("Reporte siniestro vehículo", "Telefónica", "Contacto directo", 1, 1, 1, 0),
    "pqr":                             ("PQR", "Telefónica", "Contacto directo", 1, 1, 1, 0),
    "autorizacion canales":            ("Autorización de canales", "Telefónica", "Contacto directo", 1, 1, 1, 0),
    "cliente autoriza brindar informacion a tercero":
                                       ("Autoriza información a tercero", "Telefónica", "Contacto directo", 1, 1, 1, 0),
    "agendar nueva llamada contacto":  ("Agendar nueva llamada", "Telefónica", "Contacto indirecto", 1, 1, 0, 0),
    "sin contacto":                    ("Sin contacto", "Telefónica", "No contacto", 1, 0, 0, 0),
    "buzon lleno":                     ("Buzón lleno", "Telefónica", "No contacto", 1, 0, 0, 0),
    "no se deja sobre":                ("No se deja sobre", "Notificación", "No contacto", 0, 0, 0, 0),
    "notificacion garantia mobiliaria": ("Notificación garantía mobiliaria", "Notificación", "Notificación", 0, 0, 0, 0),
    "mensaje":                         ("Mensaje", "Mensajería", "Mensaje", 0, 0, 0, 0),
    "registro soporte":                ("Registro soporte", "Administrativa", "Administrativa", 0, 0, 0, 0),
}

ORIGENES_PAGO = {
    "pse": "PSE", "banco bogota": "Banco de Bogotá", "caja": "Caja", "occidente": "Banco de Occidente",
    "reversion": "Reversión", "bancolombia": "Bancolombia", "cheque": "Cheque", "dacion": "Dación",
    "davivienda": "Davivienda", "ajuste": "Ajuste", "otros": "Otros", "av villas": "AV Villas",
    "otros sin informacion": "Otros sin información", "finandina": "Finandina",
    "colpatria": "Colpatria", "sudameris": "Sudameris",
}

ESTADOS_CARTERA = {
    "normal": "Normal", "prejudicial": "Prejudicial", "judicial": "Judicial",
    "proceso terminado": "Proceso terminado", "saldo dacion": "Saldo dación", "especial": "Especial",
    "insolvencia": "Insolvencia", "siniestro": "Siniestro", "adjudicacion": "Adjudicación",
    "0": "Sin información",
}

OPERADORES = {
    "co claro": "Claro", "claro": "Claro", "movistar": "Movistar", "tigo": "Tigo", "wom": "WOM",
    "virgin": "Virgin Mobile", "etb": "ETB", "avantel": "Avantel", "flash mobile": "Flash Mobile",
}


# --------------------------------------------------------------------------- reglas
def cuota_fija(saldo, tasa_mensual_pct, cuotas_pendientes):
    """Cuota de una anualidad: saldo * i / (1 - (1+i)^-n). La fuente no trae el valor de la cuota,
    así que se estima con el saldo, la tasa nominal mes vencido y las cuotas que faltan."""
    i = tasa_mensual_pct / 100.0
    n = cuotas_pendientes.clip(lower=1)
    con_tasa = saldo * i / (1 - (1 + i) ** (-n))
    sin_tasa = saldo / n
    return np.where(i > 0, con_tasa, sin_tasa).round(2)
