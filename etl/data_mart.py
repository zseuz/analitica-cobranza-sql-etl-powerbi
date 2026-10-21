"""Parte 1, punto 3 – Construcción del data mart (esquema mart) en Python.

Lee el modelo limpio (dw.*), arma con pandas las tablas finales que consume el dashboard
y las reescribe en SQL Server en una sola transacción (idempotente: siempre mismo resultado).

  mart.dim_fecha         calendario: años completos que cubren todas las fechas de los datos
  mart.dim_credito       1 fila por crédito: foto de cartera más reciente + último celular
  mart.fact_gestiones    1 fila por gestión, UNIFICANDO los dos canales:
                           - SMS del Reporte1 (dw.fact_sms)
                           - gestiones de cobranza de la Base 4 (dw.fact_gestion)
                         con banderas de intento / contacto / contacto directo / promesa
                         y los atributos de cartera del crédito en ese mes
  mart.fact_promesas     1 fila por promesa con su cumplimiento contra el recaudo efectivo
  mart.fact_cartera_mes  1 fila por crédito-mes (Base 3) con su gestión y recaudo del mes
  mart.fact_recaudo      1 fila por pago con origen, efectivo/reversión y gestión previa

Se ejecuta solo al terminar etl_reporte1 y cargar_bases, o a mano (desde la carpeta raíz del proyecto):
  python -m etl.data_mart
"""
import logging
import sys

import numpy as np
import pandas as pd
from sqlalchemy import text

from . import config

log = logging.getLogger("data_mart")


# --------------------------------------------------------------------------- utilidades
def _fecha(fecha_id):
    """Llave YYYYMMDD (admite nulos) -> datetime64."""
    return pd.to_datetime(fecha_id.astype("Int64").astype(str), format="%Y%m%d", errors="coerce")


def _mes_absoluto(periodo):
    """202508 -> número de meses desde el año 0 (para medir distancia entre periodos)."""
    return (periodo // 100) * 12 + periodo % 100


def foto_cercana(pares, cartera):
    """Para cada (credito_id, periodo) devuelve la foto de cartera del MISMO mes o, si no existe,
    la más cercana en el tiempo (empate -> la más reciente).

    Comité no es un panel completo: cada mes trae ~10.000 créditos casi distintos, así que
    exigir el mismo mes dejaría sin atributos de mora a 2/3 de las gestiones.
    """
    c = cartera.rename(columns={"periodo": "periodo_foto"})
    m = pares.drop_duplicates().merge(c, on="credito_id", how="inner")
    m["distancia"] = (_mes_absoluto(m["periodo_foto"]) - _mes_absoluto(m["periodo"])).abs()
    m = (m.sort_values(["credito_id", "periodo", "distancia", "periodo_foto"],
                       ascending=[True, True, True, False])
          .drop_duplicates(["credito_id", "periodo"]))
    m["foto_mismo_mes"] = m["distancia"] == 0
    return m.drop(columns="distancia")


# --------------------------------------------------------------------------- extracción
def leer_dw(cn):
    q = lambda sql: pd.read_sql(text(sql), cn)
    gest = q("""SELECT g.gestion_id, g.credito_id, g.fecha_id, g.fecha_hora, g.fecha_promesa_id,
                       g.valor_promesa, t.tipificacion, t.canal, t.categoria, t.es_intento_contacto,
                       t.es_contacto, t.es_contacto_directo
                FROM dw.fact_gestion g JOIN dw.dim_tipificacion t ON t.tipificacion_id = g.tipificacion_id""")
    sms = q("""SELECT sms_id, credito_id, fecha_id, fecha_envio, tipo_mensaje, operador, es_entregado,
                      numero_mensajes, total_clics, celular FROM dw.fact_sms""")
    rec = q("""SELECT r.recaudo_id, r.credito_id, r.fecha_id, o.origen_pago, o.es_recaudo_efectivo,
                      o.es_reversion, r.valor_total_pago, r.valor_pago_capital, r.valor_pago_intereses_mora
               FROM dw.fact_recaudo r JOIN dw.dim_origen_pago o ON o.origen_pago_id = r.origen_pago_id""")
    car = q("""SELECT credito_id, periodo, tipo_producto, estado_cartera, dias_mora, tramo_mora,
                      saldo_capital, valor_cuota_estimada FROM dw.fact_cartera_mensual""")
    cred = q("SELECT credito_id FROM dw.dim_credito")
    # Calendario: años completos que cubren todas las fechas de los datos (crece solo con los archivos nuevos)
    cal = q("""WITH fechas AS (SELECT fecha_id FROM dw.fact_gestion
                               UNION ALL SELECT fecha_promesa_id FROM dw.fact_gestion WHERE fecha_promesa_id IS NOT NULL
                               UNION ALL SELECT fecha_id FROM dw.fact_sms
                               UNION ALL SELECT fecha_id FROM dw.fact_recaudo
                               UNION ALL SELECT periodo * 100 + 1 FROM dw.fact_cartera_mensual)
               SELECT d.fecha_id, d.fecha, d.anio, d.mes, d.nombre_mes, d.periodo, d.dia, d.dia_semana,
                      d.nombre_dia, d.es_fin_semana
               FROM dw.dim_fecha AS d
               WHERE d.anio BETWEEN (SELECT MIN(fecha_id) / 10000 FROM fechas)
                                AND (SELECT MAX(fecha_id) / 10000 FROM fechas)""")

    # SQL Server devuelve DECIMAL como objetos Decimal: se pasan a float para operar
    for df, cols in [(gest, ["valor_promesa"]), (car, ["saldo_capital", "valor_cuota_estimada"]),
                     (rec, ["valor_total_pago", "valor_pago_capital", "valor_pago_intereses_mora"])]:
        df[cols] = df[cols].astype(float)
    # Tipos de fecha explícitos: con una tabla vacía (p. ej. aún sin Reporte1) pandas no los infiere
    gest["fecha_hora"] = pd.to_datetime(gest["fecha_hora"])
    sms["fecha_envio"] = pd.to_datetime(sms["fecha_envio"])
    for df, enteros in [(gest, ["gestion_id", "credito_id", "fecha_id"]),
                        (sms, ["sms_id", "credito_id", "fecha_id", "numero_mensajes", "total_clics"]),
                        (rec, ["recaudo_id", "credito_id", "fecha_id"]),
                        (car, ["credito_id", "periodo"]), (cred, ["credito_id"])]:
        df[enteros] = df[enteros].astype("int64")
    for df, logicos in [(gest, ["es_intento_contacto", "es_contacto", "es_contacto_directo"]),
                        (sms, ["es_entregado"]), (rec, ["es_recaudo_efectivo", "es_reversion"])]:
        df[logicos] = df[logicos].astype(bool)
    for df in (gest, sms, rec):
        df["fecha"] = _fecha(df["fecha_id"])
        df["periodo"] = df["fecha_id"] // 100
    return gest, sms, rec, car, cred, cal


# --------------------------------------------------------------------------- transformación
def construir_eventos(gest, sms):
    """Gestiones de los dos canales en un formato común."""
    g = pd.DataFrame({
        "gestion_key": "G-" + gest["gestion_id"].astype(str), "fuente": "Gestiones",
        "canal": gest["canal"], "credito_id": gest["credito_id"], "fecha_id": gest["fecha_id"],
        "fecha": gest["fecha"], "periodo": gest["periodo"], "fecha_hora": gest["fecha_hora"],
        "resultado": gest["tipificacion"], "categoria": gest["categoria"],
        "es_intento_contacto": gest["es_intento_contacto"], "es_contacto": gest["es_contacto"],
        "es_contacto_directo": gest["es_contacto_directo"],
        "es_promesa": gest["fecha_promesa_id"].notna(),          # promesa = trae fecha prometida
        "valor_promesa": gest["valor_promesa"], "fecha_promesa": _fecha(gest["fecha_promesa_id"]),
    })
    # SMS: no son intentos de contacto telefónico, no entran en el denominador de contactabilidad
    s = pd.DataFrame({
        "gestion_key": "S-" + sms["sms_id"].astype(str), "fuente": "Reporte1 (SMS)", "canal": "SMS",
        "credito_id": sms["credito_id"], "fecha_id": sms["fecha_id"], "fecha": sms["fecha"],
        "periodo": sms["periodo"], "fecha_hora": sms["fecha_envio"],
        "resultado": "SMS " + sms["tipo_mensaje"], "categoria": "Mensaje",
        "es_intento_contacto": False, "es_contacto": False, "es_contacto_directo": False,
        "es_promesa": False, "valor_promesa": 0.0, "fecha_promesa": pd.NaT,
        "operador": sms["operador"], "es_entregado": sms["es_entregado"].astype(bool),
        "numero_mensajes": sms["numero_mensajes"], "total_clics": sms["total_clics"],
    })
    ev = pd.concat([g, s], ignore_index=True)
    ev["es_entregado"] = ev["es_entregado"].astype("boolean")
    ev["numero_mensajes"] = ev["numero_mensajes"].astype("Int64")
    ev["total_clics"] = ev["total_clics"].astype("Int64")
    ev["hora"] = ev["fecha_hora"].dt.hour
    return ev


def construir_fact_gestiones(ev, car):
    fotos = foto_cercana(ev[["credito_id", "periodo"]], car)
    out = ev.merge(fotos, on=["credito_id", "periodo"], how="left")
    out["foto_mismo_mes"] = out["foto_mismo_mes"].fillna(False).astype(bool)
    return out.drop(columns="periodo")


def construir_dim_credito(cred, car, sms, gest, rec):
    ultima = (car.sort_values("periodo").drop_duplicates("credito_id", keep="last")
                 .rename(columns={"periodo": "periodo_ultima_foto"}))
    celular = (sms.sort_values(["fecha_envio", "sms_id"]).drop_duplicates("credito_id", keep="last")
                  [["credito_id", "celular"]])
    d = cred.merge(ultima, on="credito_id", how="left").merge(celular, on="credito_id", how="left")
    d["tiene_sms"] = d["celular"].notna()
    d["tiene_gestion"] = d["credito_id"].isin(gest["credito_id"])
    d["tiene_recaudo"] = d["credito_id"].isin(rec["credito_id"])
    return d


def construir_fact_promesas(gest, ef, car, corte):
    """Cumplimiento: recaudo EFECTIVO del crédito entre el día de la gestión y la fecha prometida
    (+ días de gracia). Cumplida si paga >= porc_cumplimiento del valor prometido; si la promesa
    no trae valor, basta cualquier pago."""
    gracia = pd.Timedelta(days=config.DIAS_GRACIA_PROMESA)
    p = gest[gest["fecha_promesa_id"].notna()].copy()
    p = pd.DataFrame({
        "gestion_id": p["gestion_id"], "credito_id": p["credito_id"], "periodo": p["periodo"],
        "fecha_gestion": p["fecha"], "fecha_gestion_id": p["fecha_id"],
        "fecha_promesa": _fecha(p["fecha_promesa_id"]), "fecha_promesa_id": p["fecha_promesa_id"].astype(int),
        "tipificacion": p["tipificacion"], "valor_promesa": p["valor_promesa"],
    })
    p["fecha_limite"] = p["fecha_promesa"] + gracia

    pagos = p[["gestion_id", "credito_id", "fecha_gestion", "fecha_limite"]].merge(
        ef[["credito_id", "fecha", "valor_total_pago"]], on="credito_id")
    pagos = pagos[(pagos["fecha"] >= pagos["fecha_gestion"]) & (pagos["fecha"] <= pagos["fecha_limite"])]
    agg = pagos.groupby("gestion_id").agg(valor_pagado=("valor_total_pago", "sum"),
                                          fecha_primer_pago=("fecha", "min")).reset_index()
    p = p.merge(agg, on="gestion_id", how="left")
    # Redondeo a centavos: un pago y su reversión deben netear 0 exacto (no 1e-11 por punto flotante)
    p["valor_pagado"] = p["valor_pagado"].fillna(0.0).round(2)

    cumple = (((p["valor_promesa"] > 0) & (p["valor_pagado"] >= p["valor_promesa"] * config.PORC_CUMPLIMIENTO_PROMESA))
              | ((p["valor_promesa"] == 0) & (p["valor_pagado"] > 0)))
    p["estado_promesa"] = np.select(
        [cumple, p["fecha_limite"] >= corte, p["valor_pagado"] > 0],
        ["Cumplida", "Vigente", "Incumplida con pago parcial"], default="Incumplida")
    p["es_vencida"] = p["fecha_limite"] < corte
    p["es_cumplida"] = cumple
    p["porc_cumplimiento"] = (p["valor_pagado"] / p["valor_promesa"]).where(p["valor_promesa"] > 0).round(4)
    p["fecha_corte"] = corte

    fotos = foto_cercana(p[["credito_id", "periodo"]], car)[["credito_id", "periodo", "dias_mora", "tramo_mora", "estado_cartera"]]
    return p.merge(fotos, on=["credito_id", "periodo"], how="left").drop(columns="periodo")


def construir_fact_cartera_mes(car, ev, ef):
    e = ev.assign(es_gestion=ev["fuente"] == "Gestiones", es_sms=ev["fuente"] != "Gestiones")
    agg = e.groupby(["credito_id", "periodo"]).agg(
        gestiones=("es_gestion", "sum"), intentos_contacto=("es_intento_contacto", "sum"),
        contactos=("es_contacto", "sum"), contactos_directos=("es_contacto_directo", "sum"),
        promesas=("es_promesa", "sum"), valor_prometido=("valor_promesa", "sum"), sms=("es_sms", "sum"),
    ).reset_index()
    rec_mes = ef.groupby(["credito_id", "periodo"])["valor_total_pago"].sum().rename("recaudo_efectivo").reset_index()

    c = car.merge(agg, on=["credito_id", "periodo"], how="left").merge(rec_mes, on=["credito_id", "periodo"], how="left")
    conteos = ["gestiones", "intentos_contacto", "contactos", "contactos_directos", "promesas", "sms"]
    c[conteos] = c[conteos].fillna(0).astype(int)
    c[["valor_prometido", "recaudo_efectivo"]] = c[["valor_prometido", "recaudo_efectivo"]].fillna(0.0)
    c["fecha_mes"] = pd.to_datetime(c["periodo"] * 100 + 1, format="%Y%m%d")
    c["es_en_mora"] = c["dias_mora"] > 0
    c["es_gestionado"] = (c["gestiones"] + c["sms"]) > 0          # cobertura
    c["es_contactado"] = c["contactos"] > 0
    c["tiene_pago"] = c["recaudo_efectivo"] > 0
    return c


def construir_fact_recaudo(rec, ev):
    """Marca si el crédito tuvo alguna gestión o SMS en los 30 días previos al pago."""
    eventos = (ev[["credito_id", "fecha"]].drop_duplicates()
                 .rename(columns={"fecha": "fecha_ultima_gestion"}).sort_values("fecha_ultima_gestion"))
    r = pd.merge_asof(rec.sort_values("fecha"), eventos, left_on="fecha", right_on="fecha_ultima_gestion",
                      by="credito_id", direction="backward")       # última gestión <= fecha del pago
    r["tuvo_gestion_previa_30d"] = ((r["fecha"] - r["fecha_ultima_gestion"]).dt.days <= 30).fillna(False)
    return r


# --------------------------------------------------------------------------- carga
COLUMNAS = {
    "mart.dim_fecha": ["fecha_id", "fecha", "anio", "mes", "nombre_mes", "periodo", "dia", "dia_semana",
                       "nombre_dia", "es_fin_semana", "fecha_mes"],
    "mart.dim_credito": ["credito_id", "periodo_ultima_foto", "tipo_producto", "estado_cartera", "dias_mora",
                         "tramo_mora", "saldo_capital", "valor_cuota_estimada", "celular", "tiene_sms",
                         "tiene_gestion", "tiene_recaudo"],
    "mart.fact_gestiones": ["gestion_key", "fuente", "canal", "credito_id", "fecha_id", "fecha", "fecha_hora",
                            "hora", "resultado", "categoria", "es_intento_contacto", "es_contacto",
                            "es_contacto_directo", "es_promesa", "valor_promesa", "fecha_promesa", "operador",
                            "es_entregado", "numero_mensajes", "total_clics", "periodo_foto", "foto_mismo_mes",
                            "tipo_producto", "estado_cartera", "dias_mora", "tramo_mora", "saldo_capital"],
    "mart.fact_promesas": ["gestion_id", "credito_id", "fecha_gestion", "fecha_gestion_id", "fecha_promesa",
                           "fecha_promesa_id", "fecha_limite", "tipificacion", "valor_promesa", "valor_pagado",
                           "fecha_primer_pago", "porc_cumplimiento", "estado_promesa", "es_vencida",
                           "es_cumplida", "dias_mora", "tramo_mora", "estado_cartera", "fecha_corte"],
    "mart.fact_cartera_mes": ["credito_id", "periodo", "fecha_mes", "tipo_producto", "estado_cartera",
                              "dias_mora", "tramo_mora", "es_en_mora", "saldo_capital", "valor_cuota_estimada",
                              "gestiones", "intentos_contacto", "contactos", "contactos_directos", "promesas",
                              "valor_prometido", "sms", "es_gestionado", "es_contactado", "recaudo_efectivo",
                              "tiene_pago"],
    "mart.fact_recaudo": ["recaudo_id", "credito_id", "fecha_id", "fecha", "origen_pago", "es_recaudo_efectivo",
                          "es_reversion", "valor_total_pago", "valor_pago_capital", "valor_pago_intereses_mora",
                          "tuvo_gestion_previa_30d"],
}


def construir(eng):
    from .db import insertar

    with eng.connect() as cn:
        gest, sms, rec, car, cred, cal = leer_dw(cn)

    # Fecha de corte: última fecha con gestiones o recaudo (no "hoy"): indicadores reproducibles
    corte = pd.Timestamp(config.FECHA_CORTE) if config.FECHA_CORTE else max(gest["fecha"].max(), rec["fecha"].max())
    ef = rec[rec["es_recaudo_efectivo"]]        # excluye Ajuste / Otros / Otros sin información

    ev = construir_eventos(gest, sms)
    cal["fecha_mes"] = pd.to_datetime(cal["fecha"]).dt.to_period("M").dt.to_timestamp()
    tablas = {
        "mart.dim_fecha": cal,
        "mart.dim_credito": construir_dim_credito(cred, car, sms, gest, rec),
        "mart.fact_gestiones": construir_fact_gestiones(ev, car),
        "mart.fact_promesas": construir_fact_promesas(gest, ef, car, corte),
        "mart.fact_cartera_mes": construir_fact_cartera_mes(car, ev, ef),
        "mart.fact_recaudo": construir_fact_recaudo(rec, ev),
    }

    with eng.begin() as cn:                       # todo o nada
        for tabla in tablas:
            cn.execute(text(f"DELETE FROM {tabla}"))
        for tabla, df in tablas.items():
            insertar(cn, df[COLUMNAS[tabla]], tabla)
    for tabla, df in tablas.items():
        log.info("%-22s %7d filas", tabla, len(df))
    return tablas


if __name__ == "__main__":
    from .db import motor
    from .etl_reporte1 import configurar_log
    configurar_log()
    construir(motor())
    sys.exit(0)
