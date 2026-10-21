"""Punto opcional – Resumen ejecutivo automático con IA generativa.

Calcula los KPIs del data mart (solo cifras AGREGADAS: ningún dato de clientes sale del
servidor), se los envía a un modelo de lenguaje y guarda la narrativa resultante en
mart.resumen_ia, que el dashboard de Power BI muestra en la página "Resumen IA".

Dos proveedores intercambiables (mismo prompt, mismo esquema de salida, misma tabla):
  * gemini (por defecto): Google Gemini API, SDK `google-genai`, plan gratuito de Google AI Studio.
        Modelo configurable con PT_GEMINI_MODEL (por defecto el alias gemini-flash-latest).
        Credencial: variable de entorno GEMINI_API_KEY.
  * claude: Anthropic Messages API, SDK `anthropic`, modelo claude-opus-5-5. Con fallbacks="default",
        si el modelo declina la solicitud la API la reintenta sola con un modelo de respaldo.
        Credencial: ANTHROPIC_API_KEY.
Ambos con salida estructurada (JSON validado contra un esquema fijo). Las claves nunca van en el código.

Uso (desde la carpeta raíz del proyecto):
  python -m Punto_Opcional_IA.resumen_ia --simular              # muestra los KPIs y el prompt, sin llamar a la API
  python -m Punto_Opcional_IA.resumen_ia                        # genera con Gemini y guarda en mart.resumen_ia
  python -m Punto_Opcional_IA.resumen_ia --proveedor claude     # genera con Claude
"""
import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime

import pandas as pd
from sqlalchemy import text

log = logging.getLogger("resumen_ia")

MODELO_CLAUDE = "claude-opus-5-5"
MODELO_GEMINI = os.getenv("PT_GEMINI_MODEL", "gemini-flash-latest")   # alias estable al Flash vigente
MODELO_GEMINI_RESPALDO = "gemini-flash-lite-latest"                   # si el principal está saturado

SISTEMA = """Eres un analista senior de cobranza de una entidad financiera colombiana (crédito de vehículo).
Escribes para la gerencia de cobranza: frases cortas, concretas y accionables, en español.
Reglas:
- Usa SOLO las cifras del JSON que recibes. No inventes datos, metas, comparativos ni causas que no se puedan
  sostener con esas cifras; si algo es una hipótesis, dilo ("podría", "sugiere").
- Cita las cifras tal como vienen, en formato colombiano: coma decimal y punto de miles ("9,4 %", "33.156"),
  montos en millones de pesos ("$1.234 M") y negativos con el signo antes del peso ("-$14.149 M").
- No propongas metas, topes ni umbrales numéricos que no estén en los datos.
- Prioriza lo que más impacta el recaudo: dónde se pierde valor en el embudo contacto → promesa → pago.
- Las alertas deben ser hallazgos que requieran acción; las recomendaciones, acciones operativas concretas
  que el equipo pueda ejecutar la próxima semana."""

ESQUEMA = {
    "type": "object",
    "properties": {
        "titular": {"type": "string", "description": "Una frase (máx. 20 palabras) con el mensaje principal"},
        "resumen": {"type": "string", "description": "Párrafo de 3 a 5 frases para la gerencia"},
        "alertas": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "titulo": {"type": "string"},
                    "detalle": {"type": "string"},
                    "severidad": {"type": "string", "enum": ["Alta", "Media", "Baja"]},
                },
                "required": ["titulo", "detalle", "severidad"],
                "additionalProperties": False,
            },
        },
        "recomendaciones": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["titular", "resumen", "alertas", "recomendaciones"],
    "additionalProperties": False,
}


# --------------------------------------------------------------------------- KPIs
def calcular_kpis(cn):
    """KPIs agregados desde el data mart (mismas definiciones que las medidas DAX del dashboard)."""
    q = lambda sql: pd.read_sql(text(sql), cn)
    g = q("""SELECT COUNT(*) AS gestiones,
                    SUM(CAST(es_intento_contacto AS INT)) AS intentos,
                    SUM(CAST(es_contacto AS INT)) AS contactos,
                    SUM(CAST(es_contacto_directo AS INT)) AS contactos_directos,
                    SUM(CAST(es_promesa AS INT)) AS promesas,
                    COUNT(DISTINCT credito_id) AS creditos_gestionados
             FROM mart.fact_gestiones""").iloc[0]
    p = q("""SELECT SUM(CAST(es_vencida AS INT)) AS vencidas,
                    SUM(CASE WHEN es_vencida = 1 AND es_cumplida = 1 THEN 1 ELSE 0 END) AS cumplidas,
                    SUM(CASE WHEN es_vencida = 1 THEN valor_promesa END) AS valor_prometido_vencidas,
                    SUM(CASE WHEN es_vencida = 1 AND es_cumplida = 1 THEN valor_promesa END) AS valor_cumplido,
                    MAX(fecha_corte) AS fecha_corte
             FROM mart.fact_promesas""").iloc[0]
    cartera = int(q("SELECT COUNT(*) AS n FROM mart.dim_credito").iloc[0]["n"])
    r = q("""SELECT SUM(CASE WHEN es_recaudo_efectivo = 1 THEN valor_total_pago END) AS recaudo_efectivo,
                    SUM(CASE WHEN es_recaudo_efectivo = 1 AND tuvo_gestion_previa_30d = 1 THEN valor_total_pago END) AS con_gestion,
                    SUM(CASE WHEN es_reversion = 1 THEN valor_total_pago END) AS reversiones
             FROM mart.fact_recaudo""").iloc[0]
    pct = lambda a, b: round(100.0 * float(a) / float(b), 1) if b else None
    mill = lambda v: round(float(v or 0) / 1e6, 0)

    por_tramo = q("""SELECT d.tramo_mora, COUNT(*) AS creditos, SUM(CAST(d.tiene_gestion AS INT)) AS con_gestion
                     FROM mart.dim_credito d GROUP BY d.tramo_mora ORDER BY d.tramo_mora""")
    por_hora = q("""SELECT hora, SUM(CAST(es_intento_contacto AS INT)) AS intentos,
                           SUM(CAST(es_contacto AS INT)) AS contactos
                    FROM mart.fact_gestiones WHERE es_intento_contacto = 1 GROUP BY hora ORDER BY hora""")
    por_tipo = q("""SELECT tipificacion, COUNT(*) AS vencidas, SUM(CAST(es_cumplida AS INT)) AS cumplidas
                    FROM mart.fact_promesas WHERE es_vencida = 1 GROUP BY tipificacion""")
    sms = q("""SELECT COUNT(*) AS sms, COUNT(DISTINCT credito_id) AS creditos,
                      SUM(CAST(es_entregado AS INT)) AS entregados, SUM(total_clics) AS clics
               FROM mart.fact_gestiones WHERE canal = 'SMS'""").iloc[0]

    return {
        "fecha_corte": str(p["fecha_corte"]),
        "embudo": {
            "gestiones_totales": int(g["gestiones"]),
            "intentos_contacto_telefonico": int(g["intentos"]),
            "tasa_contactabilidad_pct": pct(g["contactos"], g["intentos"]),
            "tasa_contacto_directo_pct": pct(g["contactos_directos"], g["intentos"]),
            "tasa_ptp_pct": pct(g["promesas"], g["contactos_directos"]),
            "promesas_vencidas": int(p["vencidas"]),
            "kept_rate_pct": pct(p["cumplidas"], p["vencidas"]),
            "valor_prometido_vencidas_millones": mill(p["valor_prometido_vencidas"]),
            "pct_valor_prometido_cumplido": pct(p["valor_cumplido"], p["valor_prometido_vencidas"]),
        },
        "cobertura": {
            "creditos_en_cartera": cartera,
            "creditos_gestionados": int(g["creditos_gestionados"]),
            "cobertura_pct": pct(g["creditos_gestionados"], cartera),
            "intensidad_gestiones_por_credito": round(float(g["gestiones"]) / float(g["creditos_gestionados"]), 1),
            "cobertura_por_tramo_mora": [
                {"tramo": t.tramo_mora, "creditos": int(t.creditos), "cobertura_pct": pct(t.con_gestion, t.creditos)}
                for t in por_tramo.itertuples()],
        },
        "contactabilidad_por_hora": [
            {"hora": int(h.hora), "intentos": int(h.intentos), "contactabilidad_pct": pct(h.contactos, h.intentos)}
            for h in por_hora.itertuples() if h.intentos >= 300],
        "cumplimiento_por_tipo_promesa": [
            {"tipo": t.tipificacion, "vencidas": int(t.vencidas), "kept_rate_pct": pct(t.cumplidas, t.vencidas)}
            for t in por_tipo.itertuples()],
        "recaudo": {
            "recaudo_efectivo_millones": mill(r["recaudo_efectivo"]),
            "pct_recaudo_con_gestion_previa_30d": pct(r["con_gestion"], r["recaudo_efectivo"]),
            "reversiones_millones": mill(r["reversiones"]),
        },
        "sms_reporte1": {
            "sms_enviados": int(sms["sms"]), "creditos": int(sms["creditos"]),
            "tasa_entrega_pct": pct(sms["entregados"], sms["sms"]), "clics": int(sms["clics"] or 0),
            "nota": "El Reporte1 es de jul-2022; las demás bases son de jun-ago 2025 (no se cruzan por fecha).",
        },
    }


def construir_mensaje(kpis):
    return ("Genera el resumen ejecutivo del tablero de cobranza a partir de estos indicadores "
            f"(fecha de corte {kpis['fecha_corte']}):\n\n"
            f"```json\n{json.dumps(kpis, ensure_ascii=False, indent=2)}\n```\n\n"
            "Entrega: un titular, un resumen de 3 a 5 frases, entre 3 y 5 alertas con severidad y "
            "entre 3 y 5 recomendaciones.")


# --------------------------------------------------------------------------- llamada a Claude
def generar_resumen_claude(kpis):
    import anthropic

    cliente = anthropic.Anthropic()          # toma ANTHROPIC_API_KEY del entorno
    try:
        resp = cliente.beta.messages.create(
            model=MODELO_CLAUDE,
            max_tokens=16000,
            system=SISTEMA,
            messages=[{"role": "user", "content": construir_mensaje(kpis)}],
            output_config={"effort": "medium",
                           "format": {"type": "json_schema", "schema": ESQUEMA}},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except anthropic.AuthenticationError:
        sys.exit("Credenciales inválidas o ausentes: configure la variable de entorno ANTHROPIC_API_KEY.")
    except anthropic.RateLimitError:
        sys.exit("Límite de solicitudes alcanzado; intente de nuevo en unos minutos.")
    except anthropic.APIStatusError as e:
        sys.exit(f"Error de la API ({e.status_code}): {e.message}")
    except anthropic.APIConnectionError:
        sys.exit("No hay conexión con la API de Anthropic.")

    if resp.stop_reason == "refusal":
        sys.exit(f"La solicitud fue declinada ({getattr(resp.stop_details, 'category', None)}).")
    if resp.stop_reason == "max_tokens":
        sys.exit("La respuesta quedó truncada (max_tokens).")
    texto = next(b.text for b in resp.content if b.type == "text")
    log.info("Respuesta de %s | tokens entrada %s, salida %s | request-id %s",
             resp.model, resp.usage.input_tokens, resp.usage.output_tokens, resp._request_id)
    return json.loads(texto), resp.model


def _sin_additional_properties(esquema):
    """Copia del esquema sin 'additionalProperties' (Gemini solo acepta un subconjunto de JSON Schema)."""
    if isinstance(esquema, dict):
        return {k: _sin_additional_properties(v) for k, v in esquema.items() if k != "additionalProperties"}
    if isinstance(esquema, list):
        return [_sin_additional_properties(v) for v in esquema]
    return esquema


def generar_resumen_gemini(kpis):
    from google import genai
    from google.genai import errors, types

    if not (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")):
        sys.exit("Falta la clave: cree una en aistudio.google.com y ejecute  setx GEMINI_API_KEY \"<su-clave>\"")
    cliente = genai.Client()                 # toma GEMINI_API_KEY del entorno
    esquema = _sin_additional_properties(ESQUEMA)   # la respuesta se valida después con validar()
    config = types.GenerateContentConfig(
        system_instruction=SISTEMA,
        temperature=0.2,                     # redacción sobria y reproducible
        response_mime_type="application/json",
        response_json_schema=esquema,
    )
    # Reintentos ante saturación (503) o límite momentáneo (429) del plan gratuito, con espera
    # creciente; si el modelo principal sigue saturado se usa el Flash-Lite como respaldo.
    resp, modelo = None, None
    for modelo in dict.fromkeys([MODELO_GEMINI, MODELO_GEMINI_RESPALDO]):
        for intento, espera in enumerate([0, 10, 30], 1):
            time.sleep(espera)
            try:
                resp = cliente.models.generate_content(model=modelo, contents=construir_mensaje(kpis), config=config)
                break
            except errors.ServerError as e:
                log.warning("Gemini %s respondió %s (intento %d/3)", modelo, e.code, intento)
            except errors.ClientError as e:
                if e.code == 429:
                    log.warning("Gemini %s: límite momentáneo (intento %d/3)", modelo, intento)
                    continue
                if e.code == 404:
                    disponibles = [m.name for m in cliente.models.list() if "flash" in m.name or "pro" in m.name]
                    sys.exit(f"Modelo {modelo} no disponible ({e.message}). "
                             f"Use PT_GEMINI_MODEL con uno de: {disponibles[:10]}")
                sys.exit(f"Error de la API de Gemini ({e.code}): {e.message}")
        if resp is not None:
            break
    if resp is None:
        sys.exit("Gemini sigue saturado después de los reintentos; ejecute de nuevo en unos minutos.")

    if not resp.text:
        motivo = resp.candidates[0].finish_reason if resp.candidates else "sin candidatos"
        sys.exit(f"Gemini no devolvió texto (finish_reason: {motivo}).")
    u = resp.usage_metadata
    log.info("Respuesta de %s | tokens entrada %s, salida %s", modelo,
             u.prompt_token_count if u else "?", u.candidates_token_count if u else "?")
    return json.loads(resp.text), modelo


def validar(resumen):
    """Valida la respuesta contra el esquema (defensa adicional a la salida estructurada)."""
    for clave in ("titular", "resumen", "alertas", "recomendaciones"):
        if clave not in resumen:
            raise ValueError(f"La respuesta no trae '{clave}'")
    alertas = []
    for a in resumen["alertas"][:5]:
        if not {"titulo", "detalle", "severidad"} <= set(a):
            raise ValueError(f"Alerta incompleta: {a}")
        sev = str(a["severidad"]).strip().capitalize()
        alertas.append({**a, "severidad": sev if sev in ("Alta", "Media", "Baja") else "Media"})
    return {"titular": str(resumen["titular"]).strip(), "resumen": str(resumen["resumen"]).strip(),
            "alertas": alertas, "recomendaciones": [str(r).strip() for r in resumen["recomendaciones"][:5]]}


# --------------------------------------------------------------------------- guardado
def a_filas(resumen, modelo, fecha_corte):
    ahora = datetime.now().replace(microsecond=0)
    filas = [("Titular", 1, None, resumen["titular"], None),
             ("Resumen", 1, None, resumen["resumen"], None)]
    filas += [("Alerta", i, a["titulo"], a["detalle"], a["severidad"]) for i, a in enumerate(resumen["alertas"], 1)]
    filas += [("Recomendación", i, None, r, None) for i, r in enumerate(resumen["recomendaciones"], 1)]
    orden_seccion = {"Titular": 1, "Resumen": 2, "Alerta": 3, "Recomendación": 4}
    return pd.DataFrame([{"generado_en": ahora, "modelo": modelo, "fecha_corte": fecha_corte,
                          "seccion": s, "orden_seccion": orden_seccion[s], "orden": o,
                          "orden_global": orden_seccion[s] * 100 + o,
                          "etiqueta": s if s in ("Titular", "Resumen") else f"{s} {o}",
                          "titulo": t, "texto": x, "severidad": sev} for s, o, t, x, sev in filas])


def guardar(eng, df):
    from Parte1_ETL_PowerBI.Utilidades_Conexion.db import insertar
    with eng.begin() as cn:
        cn.execute(text("DELETE FROM mart.resumen_ia"))      # el tablero muestra siempre el último
        insertar(cn, df, "mart.resumen_ia")


def main(argv=None):
    from Parte1_ETL_PowerBI.Utilidades_Conexion.db import motor
    from Parte1_ETL_PowerBI.Punto2_ETL_Reporte1.etl_reporte1 import configurar_log

    p = argparse.ArgumentParser(description="Resumen ejecutivo de cobranza con IA generativa")
    p.add_argument("--simular", action="store_true", help="muestra KPIs y prompt sin llamar a la API")
    p.add_argument("--proveedor", choices=["gemini", "claude"], default="gemini")
    a = p.parse_args(argv)
    configurar_log()

    eng = motor()
    with eng.connect() as cn:
        kpis = calcular_kpis(cn)
    if a.simular:
        print("=== SISTEMA ===\n" + SISTEMA + "\n\n=== MENSAJE ===\n" + construir_mensaje(kpis))
        return 0

    resumen, modelo = (generar_resumen_gemini if a.proveedor == "gemini" else generar_resumen_claude)(kpis)
    resumen = validar(resumen)
    df = a_filas(resumen, modelo, kpis["fecha_corte"])
    guardar(eng, df)
    log.info("Resumen IA guardado en mart.resumen_ia (%d filas). Titular: %s", len(df), resumen["titular"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
