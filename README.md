# Analítica de cobranza – SQL Server, ETL en Python y Power BI

Proyecto de **Inteligencia de Negocio** para una operación de cobranza:
carga y modelado de 4 bases en SQL Server, ETL diario automatizado, data mart, dashboard de KPIs en Power BI,
vistas SQL de seguimiento a promesas de pago, revisión de un script SQL y un resumen ejecutivo generado con IA.

**Tecnologías:** SQL Server (T-SQL) · Python (pandas, SQLAlchemy, pyodbc) · Power BI (PBIP, DAX) · Google Gemini API

---

## Estructura del repositorio

```
.
├── sql/                          Scripts de base de datos (se ejecutan en orden)
│   ├── 01_modelo_relacional.sql      esquemas stg / dw / etl, llaves y restricciones
│   ├── 02_tablas_data_mart.sql       tablas del data mart (esquema mart)
│   ├── 03_vistas_promesas.sql        vistas, función y procedimiento de seguimiento
│   └── 04_consulta_optimizada.sql    reestructuración propuesta de un script existente
├── etl/                          Paquete de Python
│   ├── config.py                     conexión y parámetros
│   ├── db.py                         utilidades de conexión a SQL Server
│   ├── crear_base_datos.py           crea la BD ejecutando los scripts de sql/
│   ├── cargar_bases.py               carga Recaudo, Comité y Gestiones
│   ├── etl_reporte1.py               ETL diario del Reporte1, idempotente y con rechazos
│   ├── limpieza.py                   reglas de limpieza y catálogos
│   ├── data_mart.py                  construye las tablas del data mart
│   └── resumen_ia.py                 resumen ejecutivo con Gemini (o Claude)
├── scripts/                      Automatización en Windows
│   ├── ejecutar_etl_diario.bat       ejecuta el ETL del Reporte1
│   └── programar_tarea_diaria.ps1    lo programa en el Programador de tareas
├── powerbi/                      Dashboard de Power BI
│   ├── Dashboard_Cobranza.pbip       proyecto (abrir en Power BI Desktop)
│   ├── Dashboard_Cobranza.Report/    páginas y visuales
│   ├── Dashboard_Cobranza.SemanticModel/  modelo, relaciones y medidas DAX
│   └── generar_pbip.py               regenera el proyecto
├── docs/                         Documentación
│   ├── kpis.md                       definición, DAX y utilidad de cada KPI
│   ├── descubrimientos.md            hallazgos sobre la calidad de datos y el negocio
│   ├── analisis_script_sql.md        análisis de rendimiento de un script SQL
│   └── *.docx                        versiones en Word de los documentos
├── datos/                        aquí van los Excel de origen (no incluidos)
├── Instructivo_de_uso.txt        guía paso a paso, con solución de problemas
└── requirements.txt
```

> **Datos:** los archivos Excel de origen y el `.pbix` (que guarda una copia de los datos) **no se publican**
> por ser información confidencial. Ver [`datos/README.md`](datos/README.md).

---

## Puesta en marcha rápida

Requisitos: SQL Server (Developer o Express), ODBC Driver 18 for SQL Server, Python 3.10+ y Power BI Desktop.

```bat
python -m pip install -r requirements.txt

:: 1. Crear la base de datos AnaliticaCobranza (autenticación de Windows en localhost)
python -m etl.crear_base_datos

:: 2. Cargar las bases 2, 3 y 4
python -m etl.cargar_bases

:: 3. Cargar el Reporte1 con el ETL diario
scripts\ejecutar_etl_diario.bat "datos\base 2.xlsx"

:: 4. (Opcional) Programar la carga diaria a las 06:00
powershell -ExecutionPolicy Bypass -File scripts\programar_tarea_diaria.ps1 -Hora 06:00
```

Luego abrir `powerbi/Dashboard_Cobranza.pbip` en Power BI Desktop y hacer clic en **Actualizar**.

Para otro servidor o usuario de SQL Server, editar el bloque *EDITE AQUÍ* de
[`config.py`](etl/config.py) o usar las variables de entorno `PT_SQL_SERVER`,
`PT_SQL_USER`, `PT_SQL_PASSWORD` y `PT_SQL_DRIVER`. La guía completa, con solución de problemas, está en
[`Instructivo_de_uso.txt`](Instructivo_de_uso.txt).

---

## Resumen de la solución

### Modelo relacional y carga
Esquemas `stg` (staging), `dw` (modelo dimensional) y `etl` (control y rechazos), con 13 llaves foráneas y
11 restricciones `CHECK`. Todo lo descartado queda en `etl.rechazo` con su motivo y la fila original.

| Base | Tabla | Leídas | Cargadas | Descartadas |
|---|---|---:|---:|---|
| 1 Reporte1 (SMS) | `dw.fact_sms` | 7.438 | 7.438 | 0 |
| 2 Recaudo | `dw.fact_recaudo` | 74.966 | 74.498 | 468 pagos duplicados exactos |
| 3 Comité | `dw.fact_cartera_mensual` | 30.000 | 29.760 | 240 crédito-mes repetidos |
| 4 Gestiones | `dw.fact_gestion` | 89.485 | 69.812 | 19.673 gestiones duplicadas |

Homologación de catálogos: 893 variantes de origen de pago → 16; 1.032 variantes de tipificación → 25.

### ETL diario del Reporte1
- **Idempotente:** cada archivo se identifica por su hash SHA-256; un archivo repetido se omite.
- **Sin duplicados entre archivos:** llave natural por registro con restricción `UNIQUE`; solo entran los nuevos.
- **Validación:** estructura de columnas, formatos de fecha, celulares y conteos; las filas inválidas van a `etl.rechazo`.
- **Transaccional:** staging, dimensiones y hechos en una sola transacción (rollback total si falla).
- **Auditoría:** `etl.control_archivo` registra filas leídas, rechazadas, duplicadas e insertadas por archivo.

### Data mart
Construido en Python (`data_mart.py`) sobre el esquema `mart`: `fact_gestiones`, `fact_promesas`,
`fact_cartera_mes`, `fact_recaudo`, `dim_credito` y `dim_fecha`. Calcula el cumplimiento de promesas
(pago efectivo ≥ 90 % de lo prometido), la cobertura y la gestión previa al pago.

### Dashboard de Power BI
Modelo estrella con 32 medidas DAX y páginas de Resumen ejecutivo, Promesas de pago, Canal SMS,
Definición de KPIs y Resumen IA. Valores de control: contactabilidad 9,4 %, PTP 54,5 %, kept rate 34,5 %,
cobertura 10,3 %. Detalle en [`docs/kpis.md`](docs/kpis.md).

### Vistas de seguimiento a promesas
- `seg.vw_promesas_incumplidas_3d`: promesas incumplidas de hoy, ayer y antier.
- `seg.vw_mora60_incumplidas_hoy`: créditos con más de 60 días de mora y promesa incumplida hoy.
- Excluyentes entre sí, comparten una función en línea con CTE y `OUTER APPLY`, y un procedimiento
  (`seg.usp_seguimiento_promesas`) guarda el historial de cada ejecución.
- Como los datos son de 2025, "hoy" se fija en `seg.parametro` (25-jul-2025); con `NULL` usa la fecha real.

### Revisión de un script SQL
Análisis de problemas de correctitud y rendimiento del script original y una propuesta reestructurada:
[`analisis_script_sql.md`](docs/analisis_script_sql.md).

### Resumen ejecutivo con IA
`resumen_ia.py` envía **solo KPIs agregados** (sin datos de clientes) a Gemini y recibe una salida estructurada
(titular, resumen, alertas y recomendaciones) que se guarda en `mart.resumen_ia` y se muestra en el dashboard.

```bat
setx GEMINI_API_KEY "<su-clave>"
python -m etl.resumen_ia --simular   :: muestra lo que se enviaría, sin llamar a la API
python -m etl.resumen_ia
```

---

Los hallazgos principales sobre calidad de datos y negocio están en
[`docs/descubrimientos.md`](docs/descubrimientos.md).
