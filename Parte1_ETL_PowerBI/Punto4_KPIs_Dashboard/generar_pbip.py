"""Parte 1, punto 4 – Genera el proyecto de Power BI (Dashboard_Cobranza.pbip).

Crea un proyecto PBIP (formato de texto de Power BI Desktop) con:
  * Modelo semántico (model.bim): las tablas del esquema mart y las dos vistas de la Parte 2
    (esquema seg) en modo importación desde SQL Server, relaciones en estrella y las medidas DAX.
  * Reporte (report.json): 7 páginas con tarjetas de KPI, gráficos y segmentadores.

Uso:
  python -m Parte1_ETL_PowerBI.Punto4_KPIs_Dashboard.generar_pbip
  -> abrir Dashboard_Cobranza.pbip en Power BI Desktop, Actualizar,
     y Archivo > Guardar como > .pbix.
"""
import json
import sys
import uuid
from pathlib import Path

import pyodbc

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))   # raíz del repo
from Parte1_ETL_PowerBI.Utilidades_Conexion import config  # noqa: E402

NOMBRE = "Dashboard_Cobranza"
SALIDA = Path(__file__).resolve().parent
TABLAS = ["dim_fecha", "dim_credito", "fact_gestiones", "fact_promesas", "fact_cartera_mes", "fact_recaudo",
          "resumen_ia"]   # resumen_ia: narrativa generada con IA (punto opcional)
VISTAS = ["vw_promesas_incumplidas_3d", "vw_mora60_incumplidas_hoy"]   # Parte 2 (esquema seg)
ESQUEMA = {**{t: "mart" for t in TABLAS}, **{v: "seg" for v in VISTAS}}
PERIODO_DEFECTO = ("2025-06-01", "2025-09-01")   # filtro de fecha inicial: jun–ago 2025 (fin excluido)
MEDIDAS = "_Medidas"

TIPOS = {"bigint": "int64", "int": "int64", "smallint": "int64", "tinyint": "int64",
         "decimal": "decimal", "numeric": "decimal", "float": "double",
         "date": "dateTime", "datetime": "dateTime", "datetime2": "dateTime",
         "bit": "boolean", "varchar": "string", "nvarchar": "string", "char": "string"}
FORMATOS = {"date": "dd/mm/yyyy", "datetime2": "dd/mm/yyyy hh:nn", "decimal": "#,0"}

# ============================================================================ medidas DAX
# (nombre, expresión, formato, carpeta, descripción)
G, P, R, C, D = "fact_gestiones", "fact_promesas", "fact_recaudo", "fact_cartera_mes", "dim_credito"
V1, V2 = VISTAS
COBRANZA = f'{G}[fuente] = "Gestiones"'          # gestiones de cobranza (Base 4, jun–ago 2025), sin SMS
DAX = [
    # --- Gestión ---------------------------------------------------------------------------
    ("Gestiones (todas las fuentes)", f"COUNTROWS({G})", "#,0", "1. Gestión",
     "Gestiones de cobranza (jun–ago 2025) + SMS del Reporte1 (jul-2022). Solo para ver el volumen por canal."),
    ("Gestiones de cobranza", f"CALCULATE(COUNTROWS({G}), {COBRANZA})", "#,0", "1. Gestión",
     "Gestiones de cobranza de la Base 4 (sin SMS)."),
    ("Créditos con gestión de cobranza", f"CALCULATE(DISTINCTCOUNT({G}[credito_id]), {COBRANZA})", "#,0",
     "1. Gestión", "Créditos distintos con al menos una gestión de cobranza."),
    ("Intensidad de gestión", "DIVIDE([Gestiones de cobranza], [Créditos con gestión de cobranza])", "#,0.0",
     "1. Gestión", "Gestiones de cobranza promedio por crédito gestionado."),
    ("Créditos en cartera", f"DISTINCTCOUNT({D}[credito_id])", "#,0", "1. Gestión",
     "Créditos del portafolio (Comité)."),
    ("Cobertura de cartera", "DIVIDE([Créditos con gestión de cobranza], [Créditos en cartera])", "0.0%",
     "1. Gestión", "% de la cartera que recibió al menos una gestión de cobranza."),
    ("Cobertura cartera en mora",
     f"DIVIDE(CALCULATE([Créditos con gestión de cobranza], {D}[dias_mora] > 0), CALCULATE([Créditos en cartera], {D}[dias_mora] > 0))",
     "0.0%", "1. Gestión", "% de los créditos en mora que recibieron gestión de cobranza."),
    # --- Contacto ----------------------------------------------------------------------------
    ("Intentos de contacto", f"CALCULATE(COUNTROWS({G}), {G}[es_intento_contacto] = TRUE())", "#,0", "2. Contacto",
     "Gestiones telefónicas (denominador de contactabilidad)."),
    ("Contactos", f"CALCULATE(COUNTROWS({G}), {G}[es_contacto] = TRUE())", "#,0", "2. Contacto",
     "Intentos en los que se habló con alguien (titular o tercero)."),
    ("Contactos directos", f"CALCULATE(COUNTROWS({G}), {G}[es_contacto_directo] = TRUE())", "#,0", "2. Contacto",
     "Intentos en los que se habló con el titular."),
    ("Tasa de contactabilidad", "DIVIDE([Contactos], [Intentos de contacto])", "0.0%", "2. Contacto",
     "Contactos / intentos de contacto."),
    ("Tasa de contacto directo", "DIVIDE([Contactos directos], [Intentos de contacto])", "0.0%", "2. Contacto",
     "Contactos con el titular / intentos de contacto."),
    # --- Promesas ----------------------------------------------------------------------------
    ("Promesas generadas", f"CALCULATE(COUNTROWS({G}), {G}[es_promesa] = TRUE())", "#,0", "3. Promesas",
     "Gestiones que terminaron en promesa de pago (por fecha de gestión)."),
    ("Tasa PTP", "DIVIDE([Promesas generadas], [Contactos directos])", "0.0%", "3. Promesas",
     "Promise to Pay: promesas / contactos directos."),
    ("Promesas", f"COUNTROWS({P})", "#,0", "3. Promesas", "Promesas (por fecha prometida)."),
    ("Promesas vencidas", f"CALCULATE([Promesas], {P}[es_vencida] = TRUE())", "#,0", "3. Promesas",
     "Promesas cuya fecha límite ya pasó a la fecha de corte."),
    ("Promesas cumplidas", f"CALCULATE([Promesas], {P}[es_vencida] = TRUE(), {P}[es_cumplida] = TRUE())", "#,0",
     "3. Promesas", "Promesas vencidas con pago efectivo >= 90 % de lo prometido."),
    ("Promesas incumplidas", "[Promesas vencidas] - [Promesas cumplidas]", "#,0", "3. Promesas",
     "Promesas vencidas sin pago suficiente."),
    ("Kept rate", "DIVIDE([Promesas cumplidas], [Promesas vencidas])", "0.0%", "3. Promesas",
     "% de promesas vencidas que se cumplieron."),
    ("Valor prometido", f"SUM({P}[valor_promesa])", "$#,0", "3. Promesas", "Monto total prometido."),
    ("Valor pagado sobre promesas", f"SUM({P}[valor_pagado])", "$#,0", "3. Promesas",
     "Recaudo efectivo recibido dentro de la ventana de cada promesa."),
    # --- Recaudo -----------------------------------------------------------------------------
    ("Recaudo efectivo", f"CALCULATE(SUM({R}[valor_total_pago]), {R}[es_recaudo_efectivo] = TRUE())", "$#,0",
     "4. Recaudo", "Pagos efectivos netos de reversiones (excluye Ajuste / Otros / Otros sin información)."),
    ("Recaudo con gestión previa",
     f"CALCULATE([Recaudo efectivo], {R}[tuvo_gestion_previa_30d] = TRUE())", "$#,0", "4. Recaudo",
     "Recaudo de créditos gestionados en los 30 días previos al pago."),
    ("% recaudo con gestión previa", "DIVIDE([Recaudo con gestión previa], [Recaudo efectivo])", "0.0%",
     "4. Recaudo", "Participación del recaudo asociada a la gestión."),
    ("Reversiones", f"CALCULATE(SUM({R}[valor_total_pago]), {R}[es_reversion] = TRUE())", "$#,0", "4. Recaudo",
     "Valor reversado (negativo)."),
    # --- Cartera -----------------------------------------------------------------------------
    ("Saldo de cartera", f"SUM({C}[saldo_capital])", "$#,0", "5. Cartera", "Saldo de capital (Comité)."),
    ("Saldo en mora +30", f"CALCULATE([Saldo de cartera], {C}[dias_mora] > 30)", "$#,0", "5. Cartera",
     "Saldo de créditos con más de 30 días de mora."),
    ("Índice de cartera vencida", "DIVIDE([Saldo en mora +30], [Saldo de cartera])", "0.0%", "5. Cartera",
     "ICV: saldo en mora +30 / saldo total."),
    # --- SMS (Reporte1) ----------------------------------------------------------------------
    ("SMS enviados", f"CALCULATE(COUNTROWS({G}), {G}[canal] = \"SMS\")", "#,0", "6. SMS",
     "Mensajes del Reporte1."),
    ("Créditos con SMS", f"CALCULATE(DISTINCTCOUNT({G}[credito_id]), {G}[canal] = \"SMS\")", "#,0", "6. SMS",
     "Créditos distintos contactados por SMS."),
    ("Tasa de entrega SMS",
     f"DIVIDE(CALCULATE(COUNTROWS({G}), {G}[canal] = \"SMS\", {G}[es_entregado] = TRUE()), [SMS enviados])",
     "0.0%", "6. SMS", "SMS entregados a la red / enviados."),
    ("CTR SMS", f"DIVIDE(CALCULATE(SUM({G}[total_clics]), {G}[canal] = \"SMS\"), [SMS enviados])", "0.00%",
     "6. SMS", "Clics / SMS enviados."),
    ("SMS por crédito", "DIVIDE([SMS enviados], [Créditos con SMS])", "#,0.0", "6. SMS",
     "Intensidad del canal SMS."),
    # --- Resumen IA ----------------------------------------------------------------------------
    ("IA generado",
     'IF(ISEMPTY(resumen_ia), "Sin resumen: ejecute python -m Punto_Opcional_IA.resumen_ia", '
     '"Generado por " & MAX(resumen_ia[modelo]) & " el " & FORMAT(MAX(resumen_ia[generado_en]), "dd/mm/yyyy hh:nn") '
     '& " · corte " & FORMAT(MAX(resumen_ia[fecha_corte]), "dd/mm/yyyy"))', "", "7. Resumen IA",
     "Modelo y fecha del último resumen generado con IA."),
    # --- Seguimiento de promesas (Parte 2) ---------------------------------------------------
    ("Filas Vista 1", f"COUNTROWS({V1})", "#,0", "8. Seguimiento de promesas",
     "Promesas incumplidas de los últimos 3 días (una fila por crédito y día prometido)."),
    ("Créditos Vista 2", f"COUNTROWS({V2})", "#,0", "8. Seguimiento de promesas",
     "Créditos con más de 60 días de mora y promesa incumplida hoy."),
    ("Sin celular Vista 1", f"CALCULATE(COUNTROWS({V1}), ISBLANK({V1}[telefono_celular]))", "#,0",
     "8. Seguimiento de promesas", "Filas de la Vista 1 sin celular conocido (el crédito no aparece en el Reporte1)."),
    ("Ejecución de las vistas",
     f'IF(ISEMPTY({V1}), "Sin datos", FORMAT(MAX({V1}[fecha_hora_ejecucion]), "dd/mm/yyyy hh:nn"))', "",
     "8. Seguimiento de promesas", "Fecha y hora en que se consultaron las vistas (última actualización)."),
]


FORMATO_MEDIDA = {nombre: formato for nombre, _, formato, _, _ in DAX}


# ============================================================================ modelo
def leer_columnas():
    cn = pyodbc.connect(f"DRIVER={{{config.SQL_DRIVER}}};SERVER={config.SQL_SERVER};DATABASE={config.SQL_DB};"
                        "Trusted_Connection=yes;TrustServerCertificate=yes")
    filas = cn.execute("""SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS
                          WHERE TABLE_SCHEMA IN ('mart', 'seg') ORDER BY TABLE_NAME, ORDINAL_POSITION""").fetchall()
    cols = {}
    for esquema, t, c, tipo in filas:
        if ESQUEMA.get(t) == esquema:
            cols.setdefault(t, []).append((c, tipo))
    return cols


def tabla_modelo(nombre, columnas):
    ocultas = {"fecha_id", "fecha_gestion_id", "fecha_promesa_id", "gestion_key", "gestion_id", "recaudo_id"}
    cols = []
    for c, tipo in columnas:
        col = {"name": c, "dataType": TIPOS[tipo], "sourceColumn": c, "summarizeBy": "none"}
        if tipo in FORMATOS:
            col["formatString"] = FORMATOS[tipo]
        if c in ocultas or (c == "credito_id" and nombre != "dim_credito"):
            col["isHidden"] = True
        if nombre == "dim_fecha" and c == "fecha":
            col["isKey"] = True
        if nombre == "dim_fecha" and c == "nombre_mes":
            col["sortByColumn"] = "mes"
        if nombre == "dim_fecha" and c == "nombre_dia":
            col["sortByColumn"] = "dia_semana"
        if nombre == "resumen_ia" and c == "etiqueta":
            col["sortByColumn"] = "orden_global"
        if nombre == "resumen_ia" and c in ("resumen_id", "orden_seccion", "orden", "orden_global"):
            col["isHidden"] = True
        cols.append(col)
    t = {"name": nombre, "columns": cols, "partitions": [{
        "name": nombre, "mode": "import",
        "source": {"type": "m", "expression": [
            "let",
            "    Origen = Sql.Database(ServidorSQL, BaseDatos),",
            f"    Tabla = Origen{{[Schema=\"{ESQUEMA[nombre]}\",Item=\"{nombre}\"]}}[Data]",
            "in",
            "    Tabla"]}}]}
    if nombre == "dim_fecha":
        t["dataCategory"] = "Time"
    return t


def modelo(cols):
    tablas = [tabla_modelo(t, cols[t]) for t in TABLAS + VISTAS]
    tablas.append({
        "name": MEDIDAS,
        "columns": [{"name": "Medida", "dataType": "string", "isHidden": True, "type": "calculatedTableColumn",
                     "sourceColumn": "[Medida]"}],
        "partitions": [{"name": MEDIDAS, "mode": "import",
                        "source": {"type": "calculated", "expression": "ROW(\"Medida\", BLANK())"}}],
        "measures": [{"name": n, "expression": e, "formatString": f, "displayFolder": carpeta, "description": d}
                     for n, e, f, carpeta, d in DAX],
    })
    rel = lambda desde, col, hacia, col2, activa=True: {
        "name": str(uuid.uuid4()), "fromTable": desde, "fromColumn": col, "toTable": hacia, "toColumn": col2,
        **({} if activa else {"isActive": False})}
    relaciones = [
        rel("fact_gestiones", "fecha_id", "dim_fecha", "fecha_id"),
        rel("fact_recaudo", "fecha_id", "dim_fecha", "fecha_id"),
        rel("fact_promesas", "fecha_promesa_id", "dim_fecha", "fecha_id"),
        rel("fact_promesas", "fecha_gestion_id", "dim_fecha", "fecha_id", activa=False),
        rel("fact_cartera_mes", "fecha_mes", "dim_fecha", "fecha"),
        rel("fact_gestiones", "credito_id", "dim_credito", "credito_id"),
        rel("fact_promesas", "credito_id", "dim_credito", "credito_id"),
        rel("fact_recaudo", "credito_id", "dim_credito", "credito_id"),
        rel("fact_cartera_mes", "credito_id", "dim_credito", "credito_id"),
    ]
    parametro = lambda n, v: {"name": n, "kind": "m", "expression":
                              f"\"{v}\" meta [IsParameterQuery=true, Type=\"Text\", IsParameterQueryRequired=true]"}
    return {
        "compatibilityLevel": 1567,
        "model": {
            "culture": "es-ES",
            "dataAccessOptions": {"legacyRedirects": True, "returnErrorValuesAsNull": True},
            "defaultPowerBIDataSourceVersion": "powerBI_V3",
            "sourceQueryCulture": "es-CO",
            "tables": tablas,
            "relationships": relaciones,
            "expressions": [parametro("ServidorSQL", config.SQL_SERVER), parametro("BaseDatos", config.SQL_DB)],
            "annotations": [{"name": "PBI_QueryOrder",
                             "value": json.dumps(["ServidorSQL", "BaseDatos"] + TABLAS + VISTAS)},
                            {"name": "__PBI_TimeIntelligenceEnabled", "value": "0"}],
        },
    }


# ============================================================================ reporte
def _lit(v):
    return {"expr": {"Literal": {"Value": v}}}


class Pagina:
    def __init__(self, nombre, titulo):
        self.nombre, self.titulo, self.visuales, self.z = nombre, titulo, [], 0
        self.filtros = []          # filtros de página (panel de filtros de Power BI)

    def _agregar(self, x, y, w, h, single):
        self.z += 1
        cfg = {"name": uuid.uuid4().hex[:20],
               "layouts": [{"id": 0, "position": {"x": x, "y": y, "z": self.z, "width": w, "height": h}}],
               "singleVisual": single}
        self.visuales.append({"x": x, "y": y, "z": self.z, "width": w, "height": h,
                              "config": json.dumps(cfg, ensure_ascii=False), "filters": "[]"})

    def texto(self, x, y, w, h, parrafos, fondo=None, borde=None):
        """parrafos: lista de (texto, tamaño_pt, negrita) o (texto, tamaño_pt, negrita, color_hex).
        fondo / borde: color hex del cuadro (opcionales)."""
        def estilo(p):
            t, s, b, color = (*p, None)[:4]
            return {"fontSize": f"{s}pt", **({"fontWeight": "bold"} if b else {}), **({"color": color} if color else {})}
        single = {"visualType": "textbox", "drillFilterOtherVisuals": True, "objects": {
            "general": [{"properties": {"paragraphs": [
                {"textRuns": [{"value": p[0], "textStyle": estilo(p)}]} for p in parrafos]}}]}}
        color = lambda c: {"solid": {"color": _lit(f"'{c}'")}}
        vc = {}
        if fondo:
            vc["background"] = [{"properties": {"show": _lit("true"), "color": color(fondo), "transparency": _lit("0D")}}]
        if borde:
            vc["border"] = [{"properties": {"show": _lit("true"), "color": color(borde), "radius": _lit("8D")}}]
        if vc:
            single["vcObjects"] = vc
        self._agregar(x, y, w, h, single)

    def visual(self, tipo, x, y, w, h, roles, titulo=None, objetos=None):
        """roles: {"Category": [campo], "Y": [campo], ...}; campo = ("m", nombre) o ("c", tabla, columna)."""
        alias, desde, select, proy = {}, [], [], {}
        for rol, campos in roles.items():
            proy[rol] = []
            for campo in campos:
                entidad = MEDIDAS if campo[0] == "m" else campo[1]
                prop = campo[1] if campo[0] == "m" else campo[2]
                if entidad not in alias:
                    alias[entidad] = f"t{len(alias)}"
                    desde.append({"Name": alias[entidad], "Entity": entidad, "Type": 0})
                ref = f"{entidad}.{prop}"
                clave = "Measure" if campo[0] == "m" else "Column"
                if not any(s["Name"] == ref for s in select):
                    select.append({clave: {"Expression": {"SourceRef": {"Source": alias[entidad]}}, "Property": prop},
                                   "Name": ref, "NativeReferenceName": prop})
                proy[rol].append({"queryRef": ref, **({"active": True} if rol == "Category" else {})})
        single = {"visualType": tipo, "projections": proy,
                  "prototypeQuery": {"Version": 2, "From": desde, "Select": select},
                  "drillFilterOtherVisuals": True}
        if objetos:
            single["objects"] = objetos
        if titulo:
            single["vcObjects"] = {"title": [{"properties": {"show": _lit("true"), "text": _lit(f"'{titulo}'")}}]}
        self._agregar(x, y, w, h, single)

    def tarjeta(self, x, y, w, h, medida, titulo):
        unidades = "1000000D" if FORMATO_MEDIDA.get(medida, "").startswith("$") else "1D"   # millones / completas
        self.visual("card", x, y, w, h, {"Values": [("m", medida)]}, titulo,
                    {"labels": [{"properties": {"fontSize": _lit("20D"), "labelDisplayUnits": _lit(unidades)}}],
                     "categoryLabels": [{"properties": {"show": _lit("false")}}]})

    def segmentador(self, x, y, w, h, tabla, columna, titulo, modo="Dropdown", filtro=None):
        # encabezado propio del segmentador oculto: el título del visual ya dice qué filtra
        objetos = {"data": [{"properties": {"mode": _lit(f"'{modo}'")}}],
                   "header": [{"properties": {"show": _lit("false")}}]}
        if filtro:   # selección inicial del segmentador (el usuario la puede cambiar)
            objetos["general"] = [{"properties": {"filter": {"filter": filtro}}}]
        self.visual("slicer", x, y, w, h, {"Values": [("c", tabla, columna)]}, titulo, objetos)

    def seccion(self, orden):
        return {"config": "{}", "displayName": self.titulo, "displayOption": 1,
                "filters": json.dumps(self.filtros, ensure_ascii=False),
                "height": 720.0, "width": 1280.0, "name": self.nombre, "ordinal": orden,
                "visualContainers": self.visuales}


def filtro_fechas(desde, hasta):
    """Condición desde <= fecha < hasta sobre dim_fecha[fecha]."""
    col = lambda: {"Column": {"Expression": {"SourceRef": {"Source": "d"}}, "Property": "fecha"}}
    limite = lambda tipo, fecha: {"Comparison": {"ComparisonKind": tipo, "Left": col(),
                                                 "Right": {"Literal": {"Value": f"datetime'{fecha}T00:00:00'"}}}}
    return {"Version": 2, "From": [{"Name": "d", "Entity": "dim_fecha", "Type": 0}],
            "Where": [{"Condition": {"And": {"Left": limite(2, desde),       # 2 = mayor o igual
                                             "Right": limite(3, hasta)}}}]}   # 3 = menor que


def filtro_periodo(desde, hasta):
    """Filtro de página sobre la fecha; se puede cambiar o quitar en el panel de filtros."""
    return {"name": "FiltroPeriodo", "type": "Advanced", "howCreated": 1,
            "expression": {"Column": {"Expression": {"SourceRef": {"Entity": "dim_fecha"}}, "Property": "fecha"}},
            "filter": filtro_fechas(desde, hasta)}


def encabezado(p, titulo, nota):
    p.texto(10, 0, 1260, 62, [(titulo, 16, True), (nota, 9, False)])


def segmentadores_comunes(p, con_canal=True, periodo=PERIODO_DEFECTO):
    """Fecha, canal (opcional), tramo de mora, estado y producto; el periodo inicial va como filtro de página."""
    if periodo:
        p.filtros.append(filtro_periodo(*periodo))
    otros = ([("fact_gestiones", "canal", "Canal de gestión")] if con_canal else []) + [
        ("dim_credito", "tramo_mora", "Tramo de mora"), ("dim_credito", "estado_cartera", "Estado de cartera"),
        ("dim_credito", "tipo_producto", "Producto")]
    p.segmentador(10, 62, 300, 63, "dim_fecha", "fecha", "Fecha", modo="Between")
    ancho = (1260 - 310) // len(otros)
    for i, (tabla, columna, titulo) in enumerate(otros):
        p.segmentador(320 + i * ancho, 62, ancho - 10, 63, tabla, columna, titulo)


NOTA_PERIODO = ("Periodo: jun–ago 2025 (filtro de página, se cambia en el panel Filtros). "
                "Los SMS del Reporte1 son de jul-2022 y se analizan en su propia página.")


def reporte():
    paginas = []

    # ---- 1. Resumen ejecutivo ---------------------------------------------------------------
    p = Pagina("ReportSection01", "Resumen ejecutivo")
    encabezado(p, "Gestión de cobranza – Resumen ejecutivo", NOTA_PERIODO)
    segmentadores_comunes(p)
    tarjetas = [("Gestiones de cobranza", "Gestiones de cobranza"), ("Tasa de contactabilidad", "Contactabilidad"),
                ("Tasa PTP", "Tasa PTP"), ("Kept rate", "Kept rate"),
                ("Cobertura de cartera", "Cobertura"), ("Intensidad de gestión", "Intensidad"),
                ("Recaudo efectivo", "Recaudo efectivo")]
    for i, (m, t) in enumerate(tarjetas):
        p.tarjeta(10 + i * 180, 130, 172, 95, m, t)
    p.visual("clusteredColumnChart", 10, 235, 410, 230,
             {"Category": [("c", "fact_gestiones", "canal")], "Y": [("m", "Gestiones (todas las fuentes)")]},
             "Gestiones por canal")
    p.visual("clusteredBarChart", 430, 235, 410, 230,
             {"Category": [("c", "dim_credito", "tramo_mora")], "Y": [("m", "Tasa de contactabilidad"), ("m", "Tasa PTP")]},
             "Contactabilidad y PTP por tramo de mora")
    p.visual("donutChart", 850, 235, 420, 230,
             {"Category": [("c", "fact_promesas", "estado_promesa")], "Y": [("m", "Promesas")]},
             "Estado de las promesas")
    p.visual("lineChart", 10, 475, 630, 235,
             {"Category": [("c", "dim_fecha", "fecha")], "Y": [("m", "Intentos de contacto"), ("m", "Contactos"), ("m", "Promesas generadas")]},
             "Embudo de gestión diario")
    p.visual("clusteredColumnChart", 650, 475, 620, 235,
             {"Category": [("c", "dim_fecha", "fecha")], "Y": [("m", "Recaudo efectivo")]}, "Recaudo efectivo diario")
    paginas.append(p)

    # ---- 2. Contacto y cobertura (hallazgos clave) --------------------------------------------
    p = Pagina("ReportSection06", "Contacto y cobertura")
    encabezado(p, "Contacto y cobertura de la cartera",
               "La contactabilidad depende de la hora de la llamada y la cobertura es casi igual en todos los "
               "tramos de mora. " + NOTA_PERIODO)
    segmentadores_comunes(p)
    for i, (m, t) in enumerate([("Intentos de contacto", "Intentos de contacto"),
                                ("Tasa de contactabilidad", "Contactabilidad"),
                                ("Tasa de contacto directo", "Contacto directo"),
                                ("Créditos con gestión de cobranza", "Créditos gestionados"),
                                ("Cobertura de cartera", "Cobertura"),
                                ("Cobertura cartera en mora", "Cobertura cartera en mora")]):
        p.tarjeta(10 + i * 210, 130, 200, 95, m, t)
    p.visual("lineClusteredColumnComboChart", 10, 235, 1260, 230,
             {"Category": [("c", "fact_gestiones", "hora")], "Y": [("m", "Intentos de contacto")],
              "Y2": [("m", "Tasa de contactabilidad")]},
             "Intentos (columnas) y contactabilidad (línea) por hora del día")
    p.visual("clusteredColumnChart", 10, 475, 630, 235,
             {"Category": [("c", "dim_credito", "tramo_mora")], "Y": [("m", "Cobertura de cartera")]},
             "Cobertura por tramo de mora")
    p.visual("clusteredBarChart", 650, 475, 620, 235,
             {"Category": [("c", "dim_credito", "tramo_mora")],
              "Y": [("m", "Créditos en cartera"), ("m", "Créditos con gestión de cobranza")]},
             "Créditos en cartera y créditos gestionados por tramo")
    paginas.append(p)

    # ---- 3. Promesas de pago ----------------------------------------------------------------
    p = Pagina("ReportSection02", "Promesas de pago")
    encabezado(p, "Promesas de pago (PTP) y cumplimiento", NOTA_PERIODO)
    segmentadores_comunes(p)
    for i, (m, t) in enumerate([("Promesas generadas", "Promesas generadas"), ("Tasa PTP", "Tasa PTP"),
                                ("Promesas vencidas", "Promesas vencidas"), ("Kept rate", "Kept rate"),
                                ("Valor prometido", "Valor prometido"),
                                ("Valor pagado sobre promesas", "Valor pagado")]):
        p.tarjeta(10 + i * 210, 130, 200, 95, m, t)
    p.visual("columnChart", 10, 235, 630, 230,          # columnas apiladas por estado
             {"Category": [("c", "dim_fecha", "fecha")], "Series": [("c", "fact_promesas", "estado_promesa")],
              "Y": [("m", "Promesas")]}, "Promesas por día prometido y estado")
    p.visual("clusteredBarChart", 650, 235, 620, 230,
             {"Category": [("c", "dim_credito", "tramo_mora")], "Y": [("m", "Kept rate")]}, "Kept rate por tramo de mora")
    p.visual("tableEx", 10, 475, 1260, 235,
             {"Values": [("c", "fact_promesas", "credito_id"), ("c", "fact_promesas", "fecha_gestion"),
                         ("c", "fact_promesas", "fecha_promesa"), ("c", "fact_promesas", "tipificacion"),
                         ("c", "fact_promesas", "valor_promesa"), ("c", "fact_promesas", "valor_pagado"),
                         ("c", "fact_promesas", "estado_promesa"), ("c", "fact_promesas", "dias_mora")]},
             "Detalle de promesas")
    paginas.append(p)

    # ---- 4. Seguimiento de promesas (Parte 2) -----------------------------------------------
    p = Pagina("ReportSection07", "Seguimiento de promesas (Parte 2)")
    encabezado(p, "Seguimiento diario de promesas incumplidas (Parte 2)",
               "Vistas seg.vw_promesas_incumplidas_3d (Vista 1) y seg.vw_mora60_incumplidas_hoy (Vista 2), "
               "excluyentes entre sí. Se actualizan al pulsar Actualizar; 'hoy' es la fecha de seg.parametro "
               "(25-jul-2025 en modo demostración).")
    for i, (m, t) in enumerate([("Filas Vista 1", "Vista 1: promesas incumplidas"),
                                ("Créditos Vista 2", "Vista 2: créditos mora +60"),
                                ("Sin celular Vista 1", "Vista 1 sin celular"),
                                ("Ejecución de las vistas", "Ejecución de las vistas")]):
        p.tarjeta(10 + i * 315, 65, 305, 90, m, t)
    columnas = ["credito", "telefono_celular", "valor_cuota", "numero_dias_mora", "estado_credito", "dia_promesa",
                "fecha_hora_ejecucion"]
    p.visual("tableEx", 10, 165, 1260, 195, {"Values": [("c", V2, c) for c in columnas]},
             "Vista 2 – Créditos con más de 60 días de mora y promesa incumplida hoy")
    p.visual("tableEx", 10, 370, 1260, 340, {"Values": [("c", V1, c) for c in columnas]},
             "Vista 1 – Promesas incumplidas de los últimos 3 días (incluye hoy)")
    paginas.append(p)

    # ---- 5. Canal SMS (Reporte1) ------------------------------------------------------------
    p = Pagina("ReportSection03", "Canal SMS (Reporte1)")
    encabezado(p, "Canal SMS – Reporte1 (gestiones de cobro por mensajería)",
               "Los SMS son del 25 y 26 de julio de 2022 (las demás bases son de jun–ago 2025), por eso esta "
               "página no aplica el periodo inicial. El tramo de mora es el de la foto de Comité más cercana.")
    segmentadores_comunes(p, con_canal=False, periodo=None)
    for i, (m, t) in enumerate([("SMS enviados", "SMS enviados"), ("Créditos con SMS", "Créditos con SMS"),
                                ("SMS por crédito", "SMS por crédito"), ("Tasa de entrega SMS", "Tasa de entrega"),
                                ("CTR SMS", "CTR (clics)")]):
        p.tarjeta(10 + i * 252, 130, 242, 95, m, t)
    p.visual("clusteredBarChart", 10, 235, 410, 475,
             {"Category": [("c", "fact_gestiones", "operador")], "Y": [("m", "SMS enviados")]}, "SMS por operador")
    p.visual("donutChart", 430, 235, 410, 230,
             {"Category": [("c", "fact_gestiones", "resultado")], "Y": [("m", "SMS enviados")]}, "SMS por tipo de mensaje")
    p.visual("clusteredColumnChart", 850, 235, 420, 230,
             {"Category": [("c", "fact_gestiones", "hora")], "Y": [("m", "SMS enviados")]}, "SMS por hora de envío")
    p.visual("clusteredBarChart", 430, 475, 840, 235,
             {"Category": [("c", "dim_credito", "tramo_mora")], "Y": [("m", "SMS enviados"), ("m", "Créditos con SMS")]},
             "SMS por tramo de mora")
    paginas.append(p)

    # ---- 6. Resumen IA (punto opcional) -----------------------------------------------------
    p = Pagina("ReportSection05", "Resumen IA")
    p.texto(10, 5, 1260, 50, [("Resumen ejecutivo generado con IA", 20, True)])
    p.texto(10, 55, 1260, 45, [("Narrativa generada con IA (Google Gemini por defecto) a partir de los KPIs agregados "
                                "del data mart (Punto_Opcional_IA/resumen_ia.py). Revise las cifras contra las páginas "
                                "anteriores antes de usarla.", 10, False)])
    p.tarjeta(10, 100, 1260, 60, "IA generado", "Origen")
    p.visual("tableEx", 10, 170, 1260, 540,
             {"Values": [("c", "resumen_ia", "etiqueta"), ("c", "resumen_ia", "severidad"),
                         ("c", "resumen_ia", "titulo"), ("c", "resumen_ia", "texto")]},
             "Titular, resumen, alertas y recomendaciones")
    paginas.append(p)

    # ---- 7. Definición de KPIs --------------------------------------------------------------
    p = Pagina("ReportSection04", "Definición de KPIs")
    encabezado(p, "Definición y utilidad de los KPIs",
               "Qué mide cada indicador, cómo se calcula y para qué sirve en la operación o en la gerencia.")
    ancho, alto = 413, 155
    for i, (nombre, formula, uso, es_nota) in enumerate(UTILIDAD):
        x, y = 10 + (i % 3) * (ancho + 10), 70 + (i // 3) * (alto + 6)
        acento = "#8A6D00" if es_nota else AZUL
        parrafos = [(nombre, 12, True, acento)]
        if formula:
            parrafos.append((formula, 9, True, "#595959"))
        parrafos.append((uso, 9, False, "#262626"))
        p.texto(x, y, ancho, alto, parrafos, fondo="#FFF8E1" if es_nota else "#F3F7FB",
                borde="#E6C35C" if es_nota else "#BDD3E8")
    paginas.append(p)

    return {"config": json.dumps({"version": "5.43", "themeCollection": {}, "activeSectionIndex": 0,
                                  "linguisticSchemaSyncVersion": 0}),
            "layoutOptimization": 0,
            "sections": [pg.seccion(i) for i, pg in enumerate(paginas)]}


AZUL = "#1F4E79"
UTILIDAD = [
    ("Tasa de contactabilidad", "Contactos / intentos telefónicos",
     "Mide la calidad de la base de teléfonos y la efectividad del marcador. Si baja, hay que depurar números "
     "o cambiar franjas horarias.", False),
    ("Tasa de contacto directo", "Contactos con el titular / intentos",
     "Solo el titular puede comprometerse a pagar: es el verdadero techo de la gestión telefónica.", False),
    ("Tasa PTP (promesas de pago)", "Promesas / contactos directos",
     "Mide la capacidad de negociación. Una PTP baja con contacto alto apunta a guion, capacitación u "
     "ofertas de normalización.", False),
    ("Kept rate (cumplimiento)", "Promesas cumplidas / promesas vencidas",
     "Mide la calidad de las promesas. Una PTP alta con kept rate bajo indica promesas 'de salida' que no "
     "generan caja.", False),
    ("Cobertura de cartera", "Créditos con gestión de cobranza / créditos en cartera",
     "Qué parte del portafolio se está gestionando. Sirve para dimensionar la capacidad operativa y priorizar "
     "tramos sin gestión.", False),
    ("Intensidad de gestión", "Gestiones de cobranza / créditos con gestión",
     "Esfuerzo por crédito. Comparada con el recaudo, muestra si insistir más genera pago o solo costo "
     "(y riesgo de acoso al cliente).", False),
    ("Recaudo efectivo y % con gestión previa", "Pagos efectivos netos de reversiones",
     "Resultado final en caja, sin ajustes ni 'otros'. El % con gestión previa (30 días) aproxima el aporte "
     "de la cobranza al recaudo.", False),
    ("Índice de cartera vencida (ICV)", "Saldo con mora > 30 días / saldo total",
     "Salud del portafolio y contexto para leer los demás KPIs por tramo de mora.", False),
    ("Canal SMS", "Volumen, tasa de entrega, CTR y SMS por crédito",
     "Eficiencia del canal digital de menor costo. Con 0 clics en el Reporte1, el SMS funciona como "
     "recordatorio y no como canal de autogestión.", False),
    ("Seguimiento de promesas (Parte 2)", "Vista 1 y Vista 2, excluyentes",
     "Listas de trabajo diarias: promesas incumplidas de los últimos 3 días y créditos con mora +60 y "
     "promesa incumplida hoy.", False),
    ("Nota sobre fechas y fuentes", "",
     "Gestiones, promesas y recaudo son de jun–ago 2025; los SMS del Reporte1 son de jul-2022. Por eso los "
     "KPIs de gestión no incluyen los SMS.", True),
    ("Nota sobre asesor", "",
     "Las fuentes no traen asesor ni agente. El tablero segmenta por fecha, canal, tramo de mora, estado y "
     "producto.", True),
]


# ============================================================================ escritura
def escribir():
    cols = leer_columnas()
    faltan = [t for t in TABLAS + VISTAS if t not in cols]
    if faltan:
        sys.exit(f"Faltan tablas o vistas en SQL Server: {faltan}. Ejecute crear_base_datos (paso 5 del instructivo)")

    sm, rp = SALIDA / f"{NOMBRE}.SemanticModel", SALIDA / f"{NOMBRE}.Report"
    sm.mkdir(parents=True, exist_ok=True)
    rp.mkdir(parents=True, exist_ok=True)
    plataforma = lambda tipo: {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
        "metadata": {"type": tipo, "displayName": NOMBRE},
        "config": {"version": "2.0", "logicalId": str(uuid.uuid5(uuid.NAMESPACE_DNS, NOMBRE + tipo))}}
    archivos = {
        SALIDA / f"{NOMBRE}.pbip": {"version": "1.0", "artifacts": [{"report": {"path": f"{NOMBRE}.Report"}}],
                                    "settings": {"enableAutoRecovery": True}},
        sm / ".platform": plataforma("SemanticModel"),
        sm / "definition.pbism": {"version": "1.0", "settings": {}},
        sm / "model.bim": modelo(cols),
        rp / ".platform": plataforma("Report"),
        rp / "definition.pbir": {"version": "1.0", "datasetReference": {
            "byPath": {"path": f"../{NOMBRE}.SemanticModel"}, "byConnection": None}},
        rp / "report.json": reporte(),
    }
    for ruta, contenido in archivos.items():
        ruta.write_text(json.dumps(contenido, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Proyecto generado: {SALIDA / (NOMBRE + '.pbip')}")
    print(f"  {len(TABLAS)} tablas, {len(VISTAS)} vistas, {len(DAX)} medidas, {len(reporte()['sections'])} páginas")


if __name__ == "__main__":
    escribir()
