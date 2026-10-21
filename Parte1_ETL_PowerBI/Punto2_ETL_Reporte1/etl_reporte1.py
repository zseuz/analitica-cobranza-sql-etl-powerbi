"""Parte 1, punto 2 – ETL diario del Reporte1 (gestiones de cobro por SMS).

Flujo por cada archivo que llega a  Punto2_ETL_Reporte1/entrada/reporte1/  (.xlsx o .csv):

  1. CONTROL      SHA-256 del archivo. Si ese mismo contenido ya se cargó OK, se omite
                  (re-envíos o ejecuciones repetidas no duplican el volumen de gestiones).
  2. EXTRACCIÓN   Lectura como texto (sin inferencias de Excel) y validación del esquema:
                  si falta una columna obligatoria el archivo completo se rechaza.
  3. LIMPIEZA     Estandarización de textos, fechas, números, celular e ID de crédito.
                  Las filas que violan una regla van a etl.rechazo con su motivo (no se
                  pierden ni se "arreglan" en silencio).
  4. DEDUPLICACIÓN Llave natural = crédito + celular + fecha programación + fecha envío +
                  tipo de mensaje + código corto  ->  hash_registro (UNIQUE en dw.fact_sms).
                  Se eliminan duplicados dentro del archivo y contra lo ya cargado.
  5. CARGA        En UNA transacción: staging -> dim_credito / dim_plantilla_sms -> fact_sms.
                  Si algo falla se hace rollback y el archivo queda en ERROR para reintento.
  6. CIERRE       Se registra el conteo (leídas / rechazadas / duplicadas / insertadas),
                  se mueve el archivo a procesados/ y se refresca el data mart.

Uso (desde la carpeta raíz del proyecto):
  python -m Parte1_ETL_PowerBI.Punto2_ETL_Reporte1.etl_reporte1                      # procesa la carpeta de entrada
  python -m Parte1_ETL_PowerBI.Punto2_ETL_Reporte1.etl_reporte1 ruta\\archivo.xlsx   # procesa archivos puntuales
  python -m Parte1_ETL_PowerBI.Punto2_ETL_Reporte1.etl_reporte1 --validar ...        # prueba sin cargar nada: deja las filas válidas y
                                                                                     # las rechazadas (con motivo) en CSV en logs/
  python -m Parte1_ETL_PowerBI.Punto2_ETL_Reporte1.etl_reporte1 --forzar ...         # recarga aunque el hash ya exista
"""
import argparse
import hashlib
import json
import logging
import shutil
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
from sqlalchemy import text

from ..Utilidades_Conexion import config
from ..Punto3_Procesamiento_Datos import limpieza as L

FUENTE = "REPORTE1"
COLUMNAS_OBLIGATORIAS = [
    "celular", "codigo_corto", "fecha_programacion", "fecha_envio", "estado", "mensaje",
    "operador", "credito", "campania", "numero_mensajes", "total_clics", "tipo_mensaje",
    "descripcion_perfil_historico",
]
log = logging.getLogger("etl_reporte1")


# --------------------------------------------------------------------------- extracción
def hash_archivo(ruta):
    h = hashlib.sha256()
    with open(ruta, "rb") as f:
        for bloque in iter(lambda: f.read(1 << 20), b""):
            h.update(bloque)
    return h.hexdigest()


def leer_archivo(ruta):
    """Lee todo como texto para que la limpieza decida los tipos (no Excel)."""
    if ruta.suffix.lower() == ".csv":
        df = pd.read_csv(ruta, dtype=str, sep=None, engine="python", encoding="utf-8-sig")
    else:
        df = pd.read_excel(ruta, dtype=str)
    df = L.normalizar_columnas(df)
    faltantes = [c for c in COLUMNAS_OBLIGATORIAS if c not in df.columns]
    if faltantes:
        raise ValueError(f"Estructura inválida: faltan columnas {faltantes}")
    df = df[COLUMNAS_OBLIGATORIAS].copy()
    df.insert(0, "fila_origen", range(2, len(df) + 2))     # fila de Excel (encabezado = 1)
    return df


# --------------------------------------------------------------------------- limpieza
def _texto(serie):
    """Recorta y colapsa espacios; vacío -> NA."""
    s = serie.astype("string").str.strip().str.replace(r"\s+", " ", regex=True)
    return s.where(s.fillna("") != "")


def _fecha(serie):
    """Acepta formatos mixtos en el mismo archivo: ISO (aaaa-mm-dd, con o sin hora y milisegundos),
    dd/mm/aaaa y número serial de Excel.

    El formato ISO se lee primero y por separado: con dayfirst=True, pandas leería '2022-07-05'
    como 7 de mayo (intercambia día y mes cuando el día es 12 o menor)."""
    s = _texto(serie)
    serial = pd.to_numeric(s, errors="coerce")
    texto = s.where(serial.isna())
    iso = pd.to_datetime(texto, format="ISO8601", errors="coerce")
    dia_primero = pd.to_datetime(texto.where(iso.isna()), format="mixed", dayfirst=True, errors="coerce")
    desde_excel = pd.to_datetime(serial, unit="D", origin="1899-12-30", errors="coerce")
    return iso.fillna(dia_primero).fillna(desde_excel)


def _capitalizar(serie):
    s = _texto(serie)
    return s.str[:1].str.upper() + s.str[1:]


def limpiar(df):
    """Devuelve (limpio, rechazos, duplicados_en_archivo)."""
    crudo = df.copy()
    out = pd.DataFrame({"fila_origen": df["fila_origen"]})

    out["codigo_origen"] = _texto(df["credito"]).str[:30]
    out["credito_id"] = L.credito_id(df["credito"].fillna(""))
    out["celular"] = L.celular_movil(df["celular"].fillna(""))
    out["codigo_corto"] = L.numero(df["codigo_corto"]).astype("Int64")
    out["fecha_programacion"] = _fecha(df["fecha_programacion"]).dt.floor("s")
    out["fecha_envio"] = _fecha(df["fecha_envio"]).dt.floor("s")
    out["estado_envio"] = _capitalizar(df["estado"]).fillna("Sin estado")
    out["mensaje"] = _texto(df["mensaje"])
    operador, desconocidos = L.mapear_catalogo(df["operador"], L.OPERADORES)
    if desconocidos:
        log.warning("Operadores no homologados (quedan como 'Otro'): %s", desconocidos)
    out["operador"] = operador.fillna("Otro")
    out["campania"] = _texto(df["campania"]).fillna("Sin campaña")
    out["tipo_mensaje"] = _capitalizar(df["tipo_mensaje"]).fillna("Sin tipo")
    out["perfil_historico"] = _capitalizar(df["descripcion_perfil_historico"]).fillna("Mensaje")
    out["numero_mensajes"] = L.numero(df["numero_mensajes"])
    out["total_clics"] = L.numero(df["total_clics"]).fillna(0)     # sin dato de clics = 0 clics

    # Reglas de validación: (máscara de error, motivo). Una fila puede acumular varios motivos.
    reglas = [
        (out["credito_id"].isna(), "crédito sin número"),
        (out["celular"].isna(), "celular inválido (se espera móvil 3XXXXXXXXX)"),
        (out["codigo_corto"].isna(), "código corto no numérico"),
        (out["fecha_programacion"].isna(), "fecha_programacion inválida"),
        (out["fecha_envio"].isna(), "fecha_envio inválida"),
        (out["fecha_envio"] < out["fecha_programacion"], "envío anterior a la programación"),
        (out["fecha_envio"] > pd.Timestamp.now() + pd.Timedelta(days=1), "fecha_envio futura"),
        (out["mensaje"].isna(), "mensaje vacío"),
        (~(out["numero_mensajes"] >= 1), "numero_mensajes inválido"),
        (~(out["total_clics"] >= 0), "total_clics inválido"),
    ]
    motivos = pd.Series("", index=out.index)
    for mascara, motivo in reglas:
        motivos = motivos.where(~mascara.fillna(True), motivos + "; " + motivo)
    motivos = motivos.str.lstrip("; ")
    malas = motivos != ""

    rechazos = pd.DataFrame({
        "fila_origen": out.loc[malas, "fila_origen"],
        "motivo": motivos[malas].str[:200],
        "registro": crudo.loc[malas].drop(columns="fila_origen").apply(
            lambda r: json.dumps(r.where(r.notna(), None).to_dict(), ensure_ascii=False), axis=1),
    })

    limpio = out.loc[~malas].copy()
    limpio["numero_mensajes"] = limpio["numero_mensajes"].astype(int)
    limpio["total_clics"] = limpio["total_clics"].astype(int)
    limpio["hash_registro"] = L.sha256(
        limpio["credito_id"], limpio["celular"],
        limpio["fecha_programacion"].dt.strftime("%Y-%m-%d %H:%M:%S"),
        limpio["fecha_envio"].dt.strftime("%Y-%m-%d %H:%M:%S"),
        limpio["tipo_mensaje"].str.lower(), limpio["codigo_corto"])
    limpio["hash_mensaje"] = limpio["mensaje"].map(lambda s: hashlib.sha256(s.encode("utf-8")).digest())

    dup = limpio.duplicated("hash_registro")
    return limpio.loc[~dup].reset_index(drop=True), rechazos, int(dup.sum())


# --------------------------------------------------------------------------- carga
SQL_CARGA = """
SET NOCOUNT ON;
DECLARE @insertadas INT;

-- 1) créditos nuevos (integridad referencial de fact_sms)
INSERT INTO dw.dim_credito (credito_id, fuente_alta)
SELECT DISTINCT s.credito_id, 'REPORTE1'
FROM stg.reporte1 AS s
WHERE NOT EXISTS (SELECT 1 FROM dw.dim_credito AS c WHERE c.credito_id = s.credito_id);

-- 2) plantillas de mensaje nuevas
INSERT INTO dw.dim_plantilla_sms (hash_mensaje, mensaje)
SELECT s.hash_mensaje, MIN(s.mensaje)
FROM stg.reporte1 AS s
WHERE NOT EXISTS (SELECT 1 FROM dw.dim_plantilla_sms AS p WHERE p.hash_mensaje = s.hash_mensaje)
GROUP BY s.hash_mensaje;

-- 3) hechos: solo registros que no existan (idempotente ante re-envíos y archivos solapados)
INSERT INTO dw.fact_sms (hash_registro, archivo_id, credito_id, fecha_id, plantilla_id, codigo_origen,
       celular, codigo_corto, fecha_programacion, fecha_envio, estado_envio, operador, campania,
       tipo_mensaje, perfil_historico, numero_mensajes, total_clics)
SELECT s.hash_registro, s.archivo_id, s.credito_id,
       CONVERT(INT, CONVERT(CHAR(8), s.fecha_envio, 112)), p.plantilla_id, s.codigo_origen,
       s.celular, s.codigo_corto, s.fecha_programacion, s.fecha_envio, s.estado_envio, s.operador,
       s.campania, s.tipo_mensaje, s.perfil_historico, s.numero_mensajes, s.total_clics
FROM stg.reporte1 AS s
JOIN dw.dim_plantilla_sms AS p ON p.hash_mensaje = s.hash_mensaje
WHERE NOT EXISTS (SELECT 1 FROM dw.fact_sms AS f WHERE f.hash_registro = s.hash_registro);
SET @insertadas = @@ROWCOUNT;

SELECT @insertadas;
"""


def _actualizar_control(eng, archivo_id, **campos):
    asignaciones = ", ".join(f"{k} = :{k}" for k in campos)
    with eng.begin() as cn:
        cn.execute(text(f"UPDATE etl.control_archivo SET {asignaciones}, fin = SYSDATETIME() "
                        f"WHERE archivo_id = :archivo_id"), {"archivo_id": archivo_id, **campos})


def cargar(eng, ruta, forzar=False):
    from ..Utilidades_Conexion.db import insertar   # import diferido: --validar funciona sin driver ODBC

    sha = hash_archivo(ruta)
    with eng.begin() as cn:
        ya = cn.execute(text("SELECT TOP 1 archivo_id FROM etl.control_archivo "
                             "WHERE fuente = :f AND hash_sha256 = :h AND estado = 'OK'"),
                        {"f": FUENTE, "h": sha}).scalar()
        if ya and not forzar:
            log.info("%s ya fue cargado (archivo_id=%s). Se omite.", ruta.name, ya)
            cn.execute(text("INSERT etl.control_archivo (fuente, nombre_archivo, hash_sha256, estado, fin, mensaje) "
                            "VALUES (:f, :n, :h, 'OMITIDO', SYSDATETIME(), :m)"),
                       {"f": FUENTE, "n": ruta.name, "h": sha, "m": f"Duplicado del archivo_id {ya}"})
            return "OMITIDO"
        if ya and forzar:   # libera el índice único 'una carga OK por hash'
            cn.execute(text("UPDATE etl.control_archivo SET estado = 'OMITIDO', mensaje = 'Reemplazado por recarga forzada' "
                            "WHERE archivo_id = :a"), {"a": ya})
        archivo_id = cn.execute(text(
            "INSERT etl.control_archivo (fuente, nombre_archivo, hash_sha256, estado) "
            "OUTPUT INSERTED.archivo_id VALUES (:f, :n, :h, 'EN_PROCESO')"),
            {"f": FUENTE, "n": ruta.name, "h": sha}).scalar()

    try:
        df = leer_archivo(ruta)
        limpio, rechazos, dup_archivo = limpiar(df)
        with eng.begin() as cn:                                   # una sola transacción
            cn.execute(text("TRUNCATE TABLE stg.reporte1"))
            stg = limpio.assign(archivo_id=archivo_id)
            insertar(cn, stg[["archivo_id", "fila_origen", "hash_registro", "credito_id", "codigo_origen",
                              "celular", "codigo_corto", "fecha_programacion", "fecha_envio",
                              "estado_envio", "operador", "campania", "tipo_mensaje",
                              "perfil_historico", "numero_mensajes", "total_clics",
                              "hash_mensaje", "mensaje"]], "stg.reporte1")
            insertadas = cn.execute(text(SQL_CARGA)).scalar() or 0
            if len(rechazos):
                insertar(cn, rechazos.assign(archivo_id=archivo_id, fuente=FUENTE)
                         [["archivo_id", "fuente", "fila_origen", "motivo", "registro"]], "etl.rechazo")
        duplicadas = dup_archivo + (len(limpio) - insertadas)
        _actualizar_control(eng, archivo_id, estado="OK", filas_leidas=len(df),
                            filas_rechazadas=len(rechazos), filas_duplicadas=duplicadas,
                            filas_insertadas=insertadas, mensaje=None)
        log.info("%s -> leídas %d | rechazadas %d | duplicadas %d | insertadas %d",
                 ruta.name, len(df), len(rechazos), duplicadas, insertadas)
        return "OK"
    except Exception as e:
        log.exception("Error cargando %s", ruta.name)
        _actualizar_control(eng, archivo_id, estado="ERROR", mensaje=str(e)[:4000])
        return "ERROR"


def mover_a_procesados(ruta):
    config.PROCESADOS.mkdir(parents=True, exist_ok=True)
    destino = config.PROCESADOS / f"{datetime.now():%Y%m%d_%H%M%S}_{ruta.name}"
    shutil.move(str(ruta), destino)
    return destino


def refrescar_mart(eng):
    """Reconstruye las tablas finales del dashboard (Parte 1, punto 3) en Python."""
    from ..Punto3_Procesamiento_Datos.data_mart import construir
    construir(eng)
    log.info("Data mart refrescado.")


# --------------------------------------------------------------------------- main
def configurar_log():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    config.LOGS.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(config.LOGS / f"etl_reporte1_{datetime.now():%Y%m%d}.log", encoding="utf-8"),
                  logging.StreamHandler(sys.stdout)])


def main(argv=None):
    p = argparse.ArgumentParser(description="ETL diario del Reporte1")
    p.add_argument("archivos", nargs="*", type=Path, help="archivos a procesar (por defecto: carpeta de entrada)")
    p.add_argument("--validar", action="store_true", help="prueba sin cargar nada: deja las filas válidas y rechazadas en CSV en logs/")
    p.add_argument("--forzar", action="store_true", help="recarga aunque el archivo ya se haya cargado")
    p.add_argument("--no-mover", action="store_true", help="no mover el archivo a procesados/")
    a = p.parse_args(argv)
    configurar_log()
    config.ENTRADA_REPORTE1.mkdir(parents=True, exist_ok=True)   # carpeta de llegada del archivo diario

    archivos = a.archivos or sorted(f for f in config.ENTRADA_REPORTE1.glob("*")
                                    if f.suffix.lower() in (".xlsx", ".xls", ".csv") and not f.name.startswith("~$"))
    if not archivos:
        log.info("No hay archivos en %s", config.ENTRADA_REPORTE1)
        return 0

    if a.validar:
        for ruta in archivos:
            df = leer_archivo(ruta)
            limpio, rechazos, dup = limpiar(df)
            base = config.LOGS / f"validacion_{ruta.stem}"
            limpio.drop(columns=["hash_registro", "hash_mensaje"]).to_csv(f"{base}_limpio.csv", index=False, encoding="utf-8-sig")
            rechazos.to_csv(f"{base}_rechazos.csv", index=False, encoding="utf-8-sig")
            log.info("[validar] %s -> leídas %d | rechazadas %d | duplicadas en archivo %d | válidas únicas %d",
                     ruta.name, len(df), len(rechazos), dup, len(limpio))
            if len(rechazos):
                log.info("[validar] motivos: %s", rechazos["motivo"].value_counts().to_dict())
        return 0

    from ..Utilidades_Conexion.db import motor
    eng = motor()
    errores, cargados = 0, 0
    for ruta in archivos:
        estado = cargar(eng, ruta, forzar=a.forzar)
        errores += estado == "ERROR"
        cargados += estado == "OK"
        if estado in ("OK", "OMITIDO") and not a.no_mover and ruta.parent.resolve() == config.ENTRADA_REPORTE1.resolve():
            log.info("Movido a %s", mover_a_procesados(ruta))
    if cargados:                     # el data mart solo se reconstruye si entró algún archivo nuevo
        refrescar_mart(eng)
    return 1 if errores else 0


if __name__ == "__main__":
    sys.exit(main())
