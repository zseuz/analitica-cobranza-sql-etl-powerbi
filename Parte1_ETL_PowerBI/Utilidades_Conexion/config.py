"""Configuración central del ETL.

Los datos de conexión se cambian en el bloque "EDITE AQUÍ" de abajo, o con las variables de
entorno PT_SQL_* (si una variable de entorno está definida, tiene prioridad sobre este archivo).
"""
import os
from pathlib import Path

# ============================ DATOS DE CONEXIÓN: EDITE AQUÍ ============================
# Deje USUARIO y CONTRASENA vacíos para entrar con su usuario de Windows (recomendado).
# Si escribe una contraseña aquí, no comparta este archivo con ella.
SERVIDOR = "localhost"                          # ej.: r"localhost\SQLEXPRESS" o "nombre_servidor"
USUARIO = ""                                    # usuario de SQL Server (vacío = usuario de Windows)
CONTRASENA = ""                                 # contraseña de ese usuario
DRIVER = "ODBC Driver 18 for SQL Server"        # o "ODBC Driver 17 for SQL Server"
# =======================================================================================

PARTE1 = Path(__file__).resolve().parent.parent        # carpeta Parte1_ETL_PowerBI/
RAIZ = PARTE1.parent                                 # carpeta raíz del proyecto
# Excel suministrados: por defecto en RAIZ/datos/ (ver Instructivo_de_uso.txt, paso 2);
# se puede usar otra carpeta con la variable de entorno PT_DATOS
DATOS = Path(os.getenv("PT_DATOS", RAIZ / "datos"))

# Conexión a SQL Server: variable de entorno si existe; si no, el valor del bloque de arriba
SQL_SERVER = os.getenv("PT_SQL_SERVER", SERVIDOR)
SQL_DB = "AnaliticaCobranza"                                # fijo: los scripts SQL crean y usan esta base
SQL_DRIVER = os.getenv("PT_SQL_DRIVER", DRIVER)
SQL_USER = os.getenv("PT_SQL_USER", USUARIO)            # vacío -> autenticación de Windows
SQL_PASSWORD = os.getenv("PT_SQL_PASSWORD", CONTRASENA)

# Archivos fuente
ARCHIVO_BASES = Path(os.getenv("PT_ARCHIVO_BASES", DATOS / "Bases_Cobranza.xlsx"))   # bases 2, 3 y 4

# Flujo diario del Reporte1: se dejan los archivos en ENTRADA y se mueven a PROCESADOS
ENTRADA_REPORTE1 = Path(os.getenv("PT_ENTRADA_REPORTE1", PARTE1 / "Punto2_ETL_Reporte1" / "entrada" / "reporte1"))
PROCESADOS = Path(os.getenv("PT_PROCESADOS", PARTE1 / "Punto2_ETL_Reporte1" / "procesados"))
LOGS = PARTE1 / "logs"

# Reglas de negocio
ORIGENES_NO_EFECTIVOS = {"Ajuste", "Otros", "Otros sin información"}   # indicado en la hoja Recaudo
DIAS_GRACIA_PROMESA = int(os.getenv("PT_DIAS_GRACIA_PROMESA", "0"))      # tolerancia para cumplir una promesa
PORC_CUMPLIMIENTO_PROMESA = float(os.getenv("PT_PORC_CUMPLIMIENTO", "0.9"))  # pago >= 90 % de lo prometido
FECHA_CORTE = os.getenv("PT_FECHA_CORTE")   # vacío -> última fecha con gestiones o recaudo en los datos
