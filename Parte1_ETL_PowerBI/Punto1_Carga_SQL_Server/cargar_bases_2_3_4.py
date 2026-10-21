"""Parte 1, punto 1 – Carga de las bases 2, 3 y 4 (archivo Bases_Cobranza.xlsx).

  Base 2  hoja Recaudo   -> dw.fact_recaudo         (+ dw.dim_origen_pago)
  Base 3  hoja Comite    -> dw.fact_cartera_mensual
  Base 4  hoja Gestiones -> dw.fact_gestion         (+ dw.dim_tipificacion)

Son cargas COMPLETAS e idempotentes: en una sola transacción se borran los hechos de
estas fuentes y se vuelven a insertar, así que ejecutar el proceso N veces deja siempre
el mismo resultado. Las filas descartadas quedan en etl.rechazo con su motivo.
(La Base 1 – Reporte1 – tiene su propio ETL incremental: Punto2_ETL_Reporte1/etl_reporte1.py)

Uso (desde la carpeta raíz del proyecto):
  python -m Parte1_ETL_PowerBI.Punto1_Carga_SQL_Server.cargar_bases_2_3_4             # limpia y carga las 3 bases en SQL Server
  python -m Parte1_ETL_PowerBI.Punto1_Carga_SQL_Server.cargar_bases_2_3_4 --validar   # prueba sin cargar nada: aplica la
                                                                                      # limpieza y muestra cuántas filas se
                                                                                      # cargarían y cuántas se descartarían
                                                                                      # (con el motivo)
"""
import argparse
import json
import logging
import sys
from datetime import datetime

import numpy as np
import pandas as pd
from sqlalchemy import text

from ..Utilidades_Conexion import config
from ..Punto3_Procesamiento_Datos import limpieza as L
from ..Punto2_ETL_Reporte1.etl_reporte1 import configurar_log, hash_archivo, refrescar_mart

log = logging.getLogger("cargar_bases_2_3_4")


# --------------------------------------------------------------------------- utilidades
def _json(df):
    return df.apply(lambda r: json.dumps(r.where(r.notna(), None).to_dict(), ensure_ascii=False), axis=1)


def _rechazos(crudo, mascara, motivo, fuente):
    if not mascara.any():
        return pd.DataFrame(columns=["fuente", "fila_origen", "motivo", "registro"])
    sub = crudo.loc[mascara]
    return pd.DataFrame({"fuente": fuente, "fila_origen": sub["fila_origen"],
                         "motivo": motivo, "registro": _json(sub.drop(columns="fila_origen"))})


def _leer(hoja):
    df = pd.read_excel(config.ARCHIVO_BASES, sheet_name=hoja, dtype=str)
    df = L.normalizar_columnas(df)            # quita las columnas 'Unnamed' con notas
    df.insert(0, "fila_origen", range(2, len(df) + 2))
    return df


# --------------------------------------------------------------------------- Base 3: Comité
def transformar_comite():
    crudo = _leer("Comite")
    d = pd.DataFrame({"fila_origen": crudo["fila_origen"]})
    d["codigo_origen"] = crudo["credito"].str.strip().str[:30]
    d["credito_id"] = L.credito_id(crudo["credito"])
    d["periodo"] = L.numero(crudo["ano_mes"]).astype("Int64")
    d["tipo_producto"] = L.numero(crudo["tipo_producto"]).astype("Int64")
    d["saldo_capital"] = L.numero(crudo["saldo_capital"]).round(2)
    tn = L.numero(crudo["tasa_nominal"])
    d["tasa_nominal"] = tn.where(tn > 0)                         # 0 = sin dato
    d["tasa_actual"] = L.numero(crudo["tasa_actual"])            # 16 nulos se conservan como NULL
    cp = L.numero(crudo["cuotas_pagadas"])
    d["cuotas_pagadas"] = cp.where(cp >= 0).astype("Int64")      # negativos (9) -> NULL
    d["plazo_total"] = L.numero(crudo["plazo_total"]).astype("Int64")
    estado, _ = L.mapear_catalogo(crudo["estado_cartera"], L.ESTADOS_CARTERA)   # no homologado -> rechazo
    d["estado_cartera"] = estado
    d["dias_mora"] = L.numero(crudo["numero_dias_mora"]).astype("Int64")

    reglas = [
        (d["credito_id"].isna(), "crédito sin número"),
        (~d["periodo"].between(200001, 209912).fillna(False), "periodo inválido"),
        (d["tipo_producto"].isna(), "tipo_producto inválido"),
        (~(d["saldo_capital"] >= 0), "saldo_capital inválido"),
        (~(d["plazo_total"] > 0).fillna(False), "plazo_total inválido"),
        (d["estado_cartera"].isna(), "estado_cartera no homologado"),
        (~d["dias_mora"].between(0, 999).fillna(False), "dias_mora fuera de rango"),
    ]
    rech, malas = [], pd.Series(False, index=d.index)
    for m, motivo in reglas:
        m = m & ~malas
        rech.append(_rechazos(crudo, m, motivo, "COMITE"))
        malas |= m
    d = d.loc[~malas]

    # Regla de negocio: un crédito tiene UNA foto por mes (PK credito_id + periodo).
    # Tras normalizar el ID aparecen 240 choques (p. ej. 'COD1016615B' y 'COD1016615-CTO').
    # Se conserva la foto de mayor riesgo (más días de mora y luego mayor saldo) – criterio
    # conservador para cobranza – y las demás se registran como rechazo.
    d = d.sort_values(["credito_id", "periodo", "dias_mora", "saldo_capital"],
                      ascending=[True, True, False, False])
    dup = d.duplicated(["credito_id", "periodo"])
    rech.append(_rechazos(crudo, crudo.index.isin(d.index[dup]),
                          "crédito-periodo repetido tras normalizar ID (se conserva el de mayor mora)", "COMITE"))
    d = d.loc[~dup].copy()

    # Valor de la cuota (no viene en la fuente): anualidad con saldo, tasa nominal mensual
    # y cuotas pendientes. Lo usa la Parte 2 (vistas de promesas).
    pendientes = (d["plazo_total"] - d["cuotas_pagadas"].fillna(0)).clip(lower=1).astype(float)
    d["valor_cuota_estimada"] = L.cuota_fija(d["saldo_capital"].astype(float),
                                             d["tasa_nominal"].fillna(0).astype(float), pendientes)
    return d.drop(columns="fila_origen"), pd.concat(rech, ignore_index=True), len(crudo)


# --------------------------------------------------------------------------- Base 2: Recaudo
def transformar_recaudo():
    crudo = _leer("Recaudo")
    d = pd.DataFrame({"fila_origen": crudo["fila_origen"]})
    d["codigo_origen"] = crudo["credito"].str.strip().str[:30]
    d["credito_id"] = L.credito_id(crudo["credito"])
    fecha = L.fecha_desde_entero(crudo["id_fecha_cal_mov"])
    d["fecha_id"] = L.fecha_id(fecha)
    d["origen_pago"], _ = L.mapear_catalogo(crudo["origen_pago"], L.ORIGENES_PAGO)
    for c in ["valor_total_pago", "valor_pago_intereses_mora", "valor_pago_capital"]:
        d[c] = L.numero(crudo[c]).round(2)

    reglas = [
        (d["credito_id"].isna(), "crédito sin número"),
        (d["fecha_id"].isna(), "fecha inválida"),
        (d["origen_pago"].isna(), "origen_pago no homologado"),
        (d[["valor_total_pago", "valor_pago_intereses_mora", "valor_pago_capital"]].isna().any(axis=1), "valor no numérico"),
    ]
    rech, malas = [], pd.Series(False, index=d.index)
    for m, motivo in reglas:
        m = m & ~malas
        rech.append(_rechazos(crudo, m, motivo, "RECAUDO"))
        malas |= m
    d = d.loc[~malas]

    # Duplicados exactos (mismo crédito, fecha, valores y origen ya normalizados):
    # la fuente no trae ID de transacción, se asume doble registro del mismo pago.
    llave = ["credito_id", "fecha_id", "origen_pago", "valor_total_pago",
             "valor_pago_intereses_mora", "valor_pago_capital"]
    dup = d.duplicated(llave)
    rech.append(_rechazos(crudo, crudo.index.isin(d.index[dup]), "pago duplicado exacto", "RECAUDO"))
    d = d.loc[~dup]
    return d.drop(columns="fila_origen"), pd.concat(rech, ignore_index=True), len(crudo)


# --------------------------------------------------------------------------- Base 4: Gestiones
def transformar_gestiones():
    crudo = _leer("Gestiones")
    d = pd.DataFrame({"fila_origen": crudo["fila_origen"]})
    d["codigo_origen"] = crudo["credito"].str.strip().str[:30]
    d["credito_id"] = L.credito_id(crudo["credito"])
    fecha = L.fecha_desde_entero(crudo["id_fecha_gestion"])
    d["fecha_id"] = L.fecha_id(fecha)
    hora = pd.to_timedelta(crudo["fecha_gestion_hora"].str.strip(), errors="coerce")
    d["fecha_hora"] = fecha + hora.fillna(pd.Timedelta(0))
    llave_tip = crudo["descripcion_perfil_historico"].map(L.llave_texto)
    d["tipificacion"] = llave_tip.map(lambda k: L.TIPIFICACIONES[k][0] if k in L.TIPIFICACIONES else None)
    d["fecha_promesa_id"] = L.fecha_id(L.fecha_desde_entero(crudo["id_fecha_promesa"]))
    d["valor_promesa"] = L.numero(crudo["valor_promesa"]).fillna(0).round(2)   # sin valor = 0

    reglas = [
        (d["credito_id"].isna(), "crédito sin número"),
        (d["fecha_id"].isna(), "fecha_gestion inválida"),
        (d["tipificacion"].isna(), "tipificación no homologada"),
        (d["valor_promesa"] < 0, "valor_promesa negativo"),
        (d["fecha_promesa_id"].notna() & (d["fecha_promesa_id"] < d["fecha_id"]).fillna(False),
         "promesa anterior a la gestión"),
    ]
    rech, malas = [], pd.Series(False, index=d.index)
    for m, motivo in reglas:
        m = m & ~malas
        rech.append(_rechazos(crudo, m, motivo, "GESTIONES"))
        malas |= m
    d = d.loc[~malas]

    # Duplicados exactos tras normalizar (mismo crédito, fecha-hora, tipificación y promesa):
    # inflarían el volumen de gestiones e intensidad, por eso se eliminan.
    dup = d.duplicated(["credito_id", "fecha_hora", "tipificacion", "fecha_promesa_id", "valor_promesa"])
    rech.append(_rechazos(crudo, crudo.index.isin(d.index[dup]), "gestión duplicada exacta", "GESTIONES"))
    d = d.loc[~dup]
    return d.drop(columns="fila_origen"), pd.concat(rech, ignore_index=True), len(crudo)


# --------------------------------------------------------------------------- carga
def cargar(eng, comite, recaudo, gestiones, rechazos, leidas):
    from ..Utilidades_Conexion.db import insertar

    sha = hash_archivo(config.ARCHIVO_BASES)
    with eng.begin() as cn:
        archivo_id = cn.execute(text(
            "INSERT etl.control_archivo (fuente, nombre_archivo, hash_sha256, estado) "
            "OUTPUT INSERTED.archivo_id VALUES ('BASES_2_3_4', :n, :h, 'EN_PROCESO')"),
            {"n": config.ARCHIVO_BASES.name, "h": sha}).scalar()
    try:
        with eng.begin() as cn:                                          # una sola transacción
            # Carga completa: se reemplazan los hechos de estas fuentes
            cn.execute(text("DELETE FROM dw.fact_gestion; DELETE FROM dw.fact_recaudo; "
                            "DELETE FROM dw.fact_cartera_mensual; "
                            "DELETE FROM etl.rechazo WHERE fuente IN ('COMITE','RECAUDO','GESTIONES'); "
                            "UPDATE etl.control_archivo SET estado = 'OMITIDO', mensaje = 'Reemplazado por nueva carga completa' "
                            "WHERE fuente = 'BASES_2_3_4' AND estado = 'OK';"))

            # Catálogos (solo agrega lo nuevo)
            tip = pd.DataFrame([v for v in L.TIPIFICACIONES.values()],
                               columns=["tipificacion", "canal", "categoria", "es_intento_contacto",
                                        "es_contacto", "es_contacto_directo", "es_compromiso"]).drop_duplicates("tipificacion")
            existentes = set(pd.read_sql("SELECT tipificacion FROM dw.dim_tipificacion", cn)["tipificacion"])
            insertar(cn, tip[~tip["tipificacion"].isin(existentes)], "dw.dim_tipificacion")

            org = pd.DataFrame({"origen_pago": sorted(set(L.ORIGENES_PAGO.values()))})
            org["es_recaudo_efectivo"] = (~org["origen_pago"].isin(config.ORIGENES_NO_EFECTIVOS)).astype(int)
            org["es_reversion"] = (org["origen_pago"] == "Reversión").astype(int)
            existentes = set(pd.read_sql("SELECT origen_pago FROM dw.dim_origen_pago", cn)["origen_pago"])
            insertar(cn, org[~org["origen_pago"].isin(existentes)], "dw.dim_origen_pago")

            # dim_credito: todos los créditos vistos (integridad referencial de los 3 hechos)
            nuevos = pd.concat([comite[["credito_id"]].assign(fuente_alta="COMITE"),
                                recaudo[["credito_id"]].assign(fuente_alta="RECAUDO"),
                                gestiones[["credito_id"]].assign(fuente_alta="GESTIONES")]
                               ).drop_duplicates("credito_id")
            existentes = set(pd.read_sql("SELECT credito_id FROM dw.dim_credito", cn)["credito_id"])
            insertar(cn, nuevos[~nuevos["credito_id"].isin(existentes)], "dw.dim_credito")

            # Hechos (llaves de catálogo resueltas contra la BD)
            ids_tip = dict(cn.execute(text("SELECT tipificacion, tipificacion_id FROM dw.dim_tipificacion")).all())
            ids_org = dict(cn.execute(text("SELECT origen_pago, origen_pago_id FROM dw.dim_origen_pago")).all())

            insertar(cn, comite[["credito_id", "periodo", "codigo_origen", "tipo_producto", "saldo_capital",
                                 "tasa_nominal", "tasa_actual", "cuotas_pagadas", "plazo_total",
                                 "valor_cuota_estimada", "estado_cartera", "dias_mora"]],
                     "dw.fact_cartera_mensual")
            insertar(cn, recaudo.assign(origen_pago_id=recaudo["origen_pago"].map(ids_org))
                     [["credito_id", "fecha_id", "origen_pago_id", "codigo_origen", "valor_total_pago",
                       "valor_pago_intereses_mora", "valor_pago_capital"]], "dw.fact_recaudo")
            insertar(cn, gestiones.assign(tipificacion_id=gestiones["tipificacion"].map(ids_tip))
                     [["credito_id", "fecha_id", "fecha_hora", "tipificacion_id", "codigo_origen",
                       "fecha_promesa_id", "valor_promesa"]], "dw.fact_gestion")
            if len(rechazos):
                insertar(cn, rechazos.assign(archivo_id=archivo_id)
                         [["archivo_id", "fuente", "fila_origen", "motivo", "registro"]], "etl.rechazo")

        insertadas = len(comite) + len(recaudo) + len(gestiones)
        with eng.begin() as cn:
            cn.execute(text("UPDATE etl.control_archivo SET estado='OK', fin=SYSDATETIME(), filas_leidas=:l, "
                            "filas_rechazadas=:r, filas_duplicadas=:d, filas_insertadas=:i WHERE archivo_id=:a"),
                       {"l": leidas, "r": int((~rechazos["motivo"].str.contains("duplica|repetido")).sum()),
                        "d": int(rechazos["motivo"].str.contains("duplica|repetido").sum()),
                        "i": insertadas, "a": archivo_id})
    except Exception as e:
        log.exception("Error en la carga de las bases 2-4")
        with eng.begin() as cn:
            cn.execute(text("UPDATE etl.control_archivo SET estado='ERROR', fin=SYSDATETIME(), mensaje=:m "
                            "WHERE archivo_id=:a"), {"m": str(e)[:4000], "a": archivo_id})
        raise


def main(argv=None):
    p = argparse.ArgumentParser(description="Carga de las bases 2, 3 y 4")
    p.add_argument("--validar", action="store_true", help="prueba sin cargar nada: aplica la limpieza y muestra filas válidas y descartadas")
    a = p.parse_args(argv)
    configurar_log()
    if not config.ARCHIVO_BASES.exists():
        log.error("No se encontró %s. Copie los Excel suministrados en %s o defina PT_DATOS / "
                  "PT_ARCHIVO_BASES (ver Instructivo_de_uso.txt, paso 2).", config.ARCHIVO_BASES, config.DATOS)
        return 1

    resultados = {}
    for nombre, fn in [("COMITE", transformar_comite), ("RECAUDO", transformar_recaudo),
                       ("GESTIONES", transformar_gestiones)]:
        limpio, rech, leidas = fn()
        resultados[nombre] = (limpio, rech, leidas)
        log.info("%-9s leídas %6d | válidas %6d | descartadas %6d %s", nombre, leidas, len(limpio), len(rech),
                 rech["motivo"].value_counts().to_dict() if len(rech) else "")

    if a.validar:
        return 0

    from ..Utilidades_Conexion.db import motor
    rechazos = pd.concat([r[1] for r in resultados.values()], ignore_index=True)
    eng = motor()
    cargar(eng, resultados["COMITE"][0], resultados["RECAUDO"][0], resultados["GESTIONES"][0],
           rechazos, sum(r[2] for r in resultados.values()))
    log.info("Carga de bases 2-4 terminada.")
    refrescar_mart(eng)
    return 0


if __name__ == "__main__":
    sys.exit(main())
