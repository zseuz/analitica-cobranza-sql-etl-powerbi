"""Crea (o actualiza) la base AnaliticaCobranza ejecutando los scripts SQL del proyecto en orden.

Uso (desde la carpeta raíz del proyecto):
  python -m Parte1_ETL_PowerBI.Punto1_Carga_SQL_Server.crear_base_datos
"""
from sqlalchemy import text

from ..Utilidades_Conexion import config
from ..Utilidades_Conexion.db import ejecutar_script, motor

SCRIPTS = [
    config.PARTE1 / "Punto1_Carga_SQL_Server" / "01_modelo_relacional.sql",      # Parte 1.1 – modelo relacional
    config.PARTE1 / "Punto3_Procesamiento_Datos" / "02_tablas_data_mart.sql",    # Parte 1.3 – tablas del data mart
    config.RAIZ / "Parte2_Vistas_Promesas" / "03_vistas_promesas.sql",        # Parte 2   – vistas de promesas
]


def main():
    # Se valida antes de ejecutar nada: un script faltante no debe saltarse en silencio
    faltantes = [str(r) for r in SCRIPTS if not r.exists()]
    if faltantes:
        raise FileNotFoundError(f"No se encontraron los scripts: {faltantes}")
    for ruta in SCRIPTS:
        ejecutar_script(ruta, base="master")
        print(f"OK  {ruta.name}")
    # 02_tablas_data_mart.sql recrea las tablas del mart vacías: si ya hay datos cargados, se reconstruyen
    eng = motor()
    with eng.connect() as cn:
        hay_datos = cn.execute(text("SELECT COUNT(*) FROM dw.dim_credito")).scalar()
    if hay_datos:
        from ..Punto3_Procesamiento_Datos.data_mart import construir
        construir(eng)
        print("OK  data mart reconstruido")


if __name__ == "__main__":
    main()
