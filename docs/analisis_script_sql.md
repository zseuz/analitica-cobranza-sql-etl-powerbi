# Parte 3 — Revisión del script `consulta_original.sql`

> Las referencias `Lnn` corresponden a los números de línea del script original (464 líneas).

---

## 1. ¿Qué hace el código? (visión general)

Es el **proceso batch diario que alimenta el data mart de cobranza** (`[Cobranza].[dbo]`) a partir del data warehouse corporativo, al que se accede por *linked server* (`[DW_FZ_38].[DW_FZ]`). En cada ejecución **reprocesa completo el mes anterior y el mes en curso**: borra ese rango y lo vuelve a insertar (patrón *delete + insert*).

Construye cinco tablas persistentes, en este orden:

| # | Tabla destino | Granularidad | Contenido |
|---|---|---|---|
| 1 | `asig_hist_dia` | crédito × día | Foto diaria de la cartera asignada: saldos, cuotas, mora, fechas clave y calendario. |
| 2 | `asig_hist_dia_agrup` | crédito × mes × día hábil de meta | Foto **inicial**, **final** y **máxima** del mes por crédito, enriquecida con segmentaciones (etapa de cobro, rangos), datos del vehículo, codeudores, demografía y asignación a campaña/cobrador. Calcula si la mora **mejoró, permaneció o se deterioró**. |
| 3 | `pagos` | transacción | Recaudos del DW (último mes) más los pagos PSE de los últimos 5 días, tomados de un API (`DW_Apis.SG.SignatureData`). |
| 4 | `pago_hist_dia` | pago × día | Pagos con atributos de calendario y número de día hábil del mes. |
| 5 | `asig_hist_mes` | crédito × mes | Resumen mensual: banderas de cambio de producto, etapa, alivios, créditos nuevos, deterioro/mejora de mora y capital en mora >30/60/90. |

En términos de negocio, el proceso responde preguntas como: *¿qué cartera tenía asignada cada cobrador este mes?*, *¿cuánto había que cobrar (`valcobrado`)?*, *¿qué créditos mejoraron o se deterioraron de tramo?* y *¿cuánto se recaudó por día hábil?* Son los insumos típicos para medir el cumplimiento de metas de cobranza.

---

## 2. ¿Cómo lo hace? (paso a paso, técnicas y funciones)

### Bloque A — Foto diaria de cartera (L2–L37)
1. `DELETE` de `asig_hist_dia` desde el primer día del mes anterior (L2–4). Esa fecha se arma con `CONCAT(YEAR(...),'-',LEFT(RIGHT(CAST(... AS date),5),2),'-01')`: el mes se extrae como texto con `RIGHT/LEFT`.
2. `INSERT ... SELECT` desde `Fact_Cartera_Dia` (L6–28). Hace **5 joins a `Dim_Fecha`** (fecha de foto, desembolso, vencimiento, inicio de mora y próxima cuota) y uno a la tabla local de calendario `fecha`. Todos llevan `WITH (NOLOCK)`.
3. Depuración (L30–37): borra los días marcados como especiales en `fecha_medicion` (`marcaespecialmeta = 1`) y el producto 85 (Judicial-EQ).

### Bloque B — Registros sintéticos de "paz y salvo" (L39–76)
Detecta los créditos cuya **última foto del mes es anterior al último día del mes**, es decir, que dejaron de aparecer en la cartera porque se cancelaron. Para cada uno crea un registro en el **día siguiente** (`fechapazysalvo = fecha + 1`) con **capital 0**. Así el crédito aparece como pagado y no simplemente "desaparece".
- Técnicas: subconsultas anidadas con `MAX(fechametadiaria)` por crédito-mes, cálculo del último día del mes con `DATEADD(dd, -DAY(DATEADD(mm,1,f)), DATEADD(mm,1,f))` (equivale a `EOMONTH`), `CASE` y una tabla temporal `#asighistdiacapital0` creada con `SELECT ... INTO`.

### Bloque C — Enriquecimiento y segmentación (L81–154)
Crea `#asig_hist_dia_rango` con cerca de 15 `CASE` que traducen códigos a etiquetas de negocio:
- **Producto** (10 = Vehículo FZ, 35 = Titularizada, 60 = Castigados…), **senda** (Regular / Pre-jurídico / Jurídico / CL-Especial) y **sucursal** (114 = Bogotá…).
- **Etapa de cobro**: los créditos al día se clasifican como *cobro preventivo* según el mes de la próxima cuota y los vencidos como *cobro administrativo* por tramo de mora.
- **`valcobrado`**: lo que hay que cobrar. Si está vencido, es vencido + interés de mora; si está al día, es la próxima cuota + seguros.
- **Rangos**: mora, antigüedad de desembolso, vencimiento e inicio de mora (`DATEDIFF(MONTH, ..., GETDATE())`), plazo, monto desembolsado, cuota y capital.
- Se une con `#asig_hist_multiprod` para traer el día hábil de meta y el tipo de portafolio.

### Bloque D — Fotos inicial, máxima y de valores (L156–213)
- `#asig_hist_dia_ini`: primera foto del mes (`MIN(fechametadiaria)`) más la asignación del mes a campaña, director, líder y usuario (`asig_mes_cobrador`). Si un crédito no tiene asignación, `ISNULL(..., 'Cobranza Digital')` lo deja en la campaña digital.
- `#asig_hist_dia_max`: `MAX`/`MIN` de cada columna por crédito-mes.
- `#asig_hist_dia_val`: por crédito, mes y día hábil de meta. Calcula el tramo de mora (0/30/60/90/180/360/999) y los montos condicionados a mora ≥30, ≥60 y ≥90 (`CASE WHEN MAX(diasmora) >= 30 THEN MAX(valcapital) ELSE 0 END`), que sirven para indicadores tipo *ICV* (índice de cartera vencida).

### Bloque E — Dimensiones auxiliares (L214–279)
- `#asig_hist_dia_cod`: oficial del crédito, tipo y subtipo de préstamo, y **número de avalistas** (suma de 5 `CASE` sobre 5 joins a `Dim_Persona`).
- `#view_cartera_ubicacion_demografica` y `#..._agrup`: departamento, ocupación, rango de edad y sexo del cliente, tomando su crédito más reciente. Para quedarse con una sola fila por cliente usa tres niveles de subconsultas `MAX` anidadas.
- `#asig_hist_dia_veh`: marca y color homologados, antigüedad del modelo, licencia, tasas, calificación de cartera y los datos demográficos.

### Bloque F — Tabla agrupada (L281–343)
`DELETE` del rango e `INSERT` del cruce `ini` + `fin` + `max` + `val` + `veh` + `cod`. Para cada atributo deja las versiones `...ini`, `...fin` y `...det` (máxima). Con `LAG(rangomoraestado) OVER (ORDER BY mes, crédito, día hábil)` clasifica la mora como **Mejorado / Permanecido / Deteriorado**. Usa un `GROUP BY` de más de 100 columnas.

### Bloque G — Pagos (L345–412)
1. Borra e inserta en `pagos` el último mes desde `Fact_Recaudo` (L345–360).
2. Borra los **últimos 5 días** e inserta los pagos del API `SignatureData` con códigos de transacción 36, 37, 81, 82, 83, 86 y 88. A esos pagos les asigna sucursal 114 y origen `'28F PAG PSE 281'` fijos (L362–379).
3. Reconstruye `pago_hist_dia` con el calendario (L381–397).
4. Calcula el **número de día hábil** del mes con una suma acumulada `SUM(diahabil) OVER (PARTITION BY mespago ORDER BY fecha)` y lo actualiza con `UPDATE ... FROM ... JOIN` (L399–412).

### Bloque H — Resumen mensual (L414–462)
Desde `asig_hist_dia_agrup` construye `asig_hist_mes` con **banderas binarias**: cambio de producto, cambio de etapa, crédito con alivio, crédito nuevo (`mesdesembolso = mes`), deterioro/permanencia/mejora y reducción de capital. También separa el capital en mora >30, >60 y >90 al inicio y al final del mes.

**Técnicas usadas en general:** linked server con nombres de 4 partes, `SELECT ... INTO` a tablas `#temporales`, `IF OBJECT_ID('tempdb..#x') IS NOT NULL DROP`, patrón *delete + insert* por ventana de fechas, `CASE` para reglas de negocio, agregaciones `MIN/MAX` para fotos inicial y máxima, funciones de ventana (`LAG`, `SUM() OVER`), `ISNULL` como valor por defecto, `DISTINCT` para quitar duplicados y `NOLOCK` para no bloquear.

---

## 3. Aspectos relevantes identificados

### 3.1 Errores funcionales (afectan el resultado)

| # | Línea | Hallazgo | Impacto |
|---|---|---|---|
| 1 | L153, L324 | Se usan `#asig_hist_multiprod` y `#asig_hist_dia_fin`, pero **ninguna se crea en el script**. | Ejecutado de forma aislada, el script **falla** (`Invalid object name`). Depende de código que no está o de una sesión previa; sin la foto *fin* no se pueden calcular las comparaciones inicio/fin. |
| 2 | L314–316 | `LAG(...) OVER (ORDER BY mes, crédito, día)` **sin `PARTITION BY numerocredito`**. | La primera fila de cada crédito se compara con la última del crédito **anterior**, así que "Mejorado/Deteriorado" queda mal en cada cambio de crédito. |
| 3 | L91 | `'PREJUDICAL'` (falta la I). Además el `ELSE` cae en `'C.Jurídico'`. | Los créditos `PREJUDICIAL` y cualquier estado nuevo o mal escrito se clasifican **silenciosamente como jurídicos**. |
| 4 | L426 | En `tipoaliviofin` se evalúa `marcacionalivioini = 88` en lugar de `marcacionaliviofin`. | Error de copiar y pegar: el alivio final "Plan Choque" sale mal. |
| 5 | L429 | `CASE WHEN ... THEN 0 ELSE 0 END` en `creditosdifsenda`. | La bandera **siempre vale 0**: el indicador de cambio de senda no funciona. |
| 6 | L362–379 | Después de cargar el último mes desde `Fact_Recaudo`, borra **todos** los pagos de los últimos 5 días y vuelve a insertar **solo** los del API PSE. | Se pierden los pagos de caja, bancos y cheques de los últimos 5 días: el **recaudo reciente queda subestimado**. |
| 7 | L348 vs L4 | Los pagos usan `DATEADD(mm,-1,GETDATE())` (un mes móvil **con hora**); el resto usa el primer día del mes anterior. | Las ventanas no coinciden: la ventana de pagos empieza a la hora de ejecución, y lo insertado antes de ese punto puede **duplicarse o quedar con huecos**. |
| 8 | L233 | Edad: `<=28`, `29–38`, `39–48`, `49–58`, `>59`. | **59 años cae en 'N/A'**, aunque la etiqueta dice `E.>58`. |
| 9 | L146–148 | `rangocuotacapitalinteres`: `<=0`, luego `BETWEEN 1 AND 500000`… | Una cuota entre 0 y 1 (decimales) cae en el `ELSE` → `'E.> 2.5m'`. |
| 10 | L265–266 | Antigüedad del vehículo: el tramo `BETWEEN 2 AND 3` se etiqueta `'C.<> 3-4 años'` y `4–6` como `'5-7 años'`. | Las etiquetas no corresponden a la regla. |
| 11 | L141–142 | `tiposucursal`: `ELSE 'A.Bogota'`. | Las sucursales desconocidas se reportan como Bogotá. |
| 12 | L127–140 | Los rangos de antigüedad (desembolso, vencimiento, inicio de mora) se calculan contra `GETDATE()`, no contra la fecha de la foto. | Los meses ya procesados cambian de rango cada vez que se recalculan: el histórico **no es reproducible**. |
| 13 | L179–190 | `#asig_hist_dia_max` toma `MAX`/`MIN` de **cada columna por separado**. | La fila "det" mezcla valores de días distintos (p. ej. la mora máxima de un día con el capital de otro): no corresponde a ninguna foto real. |
| 14 | L84 | `CAST(numeroiddeudor AS int)`. | Un ID mayor a 2.147.483.647 (NIT, pasaporte) provoca un **error de desbordamiento** que detiene todo el proceso. |
| 15 | L65 / L67 | El alias `uf` se usa para **dos tablas derivadas distintas**, una anidada dentro de la otra. | Es válido por el alcance de cada una, pero es muy confuso y propenso a errores al mantenerlo. |
| 16 | L66–67 | `LEFT JOIN fecha` seguido de `INNER JOIN fecha_medicion` sobre la columna de ese left join. | El `LEFT` queda convertido en `INNER` en la práctica. |

### 3.2 Integridad y manejo de errores
- **No hay transacciones ni `TRY...CATCH`.** Cada `DELETE` se confirma solo. Si el `INSERT` siguiente falla (por ejemplo, se cae el linked server), **el mes anterior y el actual quedan borrados** y el tablero muestra cero.
- **No hay bitácora**: no se registran filas leídas o insertadas, duración ni errores. No hay forma de auditar una corrida.
- **`WITH (NOLOCK)` en todas partes**: puede leer filas sin confirmar, duplicadas o faltantes si el DW se está cargando al mismo tiempo. Sobre la **tabla destino de un `DELETE`/`UPDATE`** (L31, L36, L409) el hint se ignora y está marcado como obsoleto. Sobre tablas `#temporales` no tiene efecto.
- `INSERT INTO ... SELECT *` (L74, L351) **sin lista de columnas**: cualquier cambio en el orden o la cantidad de columnas rompe la carga o, peor, inserta valores en la columna equivocada sin dar error.
- `SELECT DISTINCT` (L158, L216, L246, L263) usado para **ocultar duplicados** en lugar de resolver la llave del join.
- Las reglas de negocio están **escritas a mano** en el código: producto 85, sucursal 114, códigos de transacción, cortes de los rangos y nombres de sucursales.
- `GETDATE()` se evalúa en cada sentencia. Si la corrida cruza la medianoche del último día del mes, **las ventanas cambian a mitad del proceso**.

### 3.3 Rendimiento y escalabilidad
- **Consultas entre servidores sin filtrar en origen**: unir tablas remotas (`DW_FZ_38`) con locales (`Cobranza.dbo.fecha`) obliga a traer tablas completas por la red. `Dim_Fecha` remota se une **hasta 5 veces** en la misma consulta. `Fact_Cartera_Master` se lee **dos veces completa** (L222, L276) con 6 joins a `Dim_Persona`, sin filtrar por los créditos del periodo.
- **Filtros que no aprovechan índices** (*no sargables*): `CAST(ah.mesmetadiaria AS date) >= ...` (L4), `CAST(df.fecha AS date) >= ...` (L348) y `CAST(CONCAT(YEAR(df.fecha)...)) >= ...` (L396). Aplicar funciones a la columna impide usar índices.
- **La misma expresión de fecha se repite unas 20 veces**, construida con texto (`CONCAT/LEFT/RIGHT`). Basta `DATEADD(MONTH, DATEDIFF(MONTH, 0, @hoy) - 1, 0)` o `DATEFROMPARTS`, calculado **una sola vez** en una variable.
- `DELETE` con `LEFT JOIN` a `Dim_Fecha` remota (L345–348, L362–365) para filtrar por fecha, cuando la tabla ya tiene `fecha_efect_movimiento` como entero `YYYYMMDD`: basta con `WHERE fecha_efect_movimiento >= @id_fecha_ini`.
- `GROUP BY` de más de 100 columnas (L330–343, L451–462) **sin funciones de agregación reales** (en L451 equivale a un `DISTINCT`). Es un costo de ordenamiento muy alto solo para quitar los duplicados que generó el cruce con `#asig_hist_dia_val` (granularidad por día hábil).
- **Temporales grandes sin índices** antes de unirlas entre sí por (`mesmetadiaria`, `numerocredito`).
- Deduplicación demográfica con **tres niveles de `MAX` anidados** (L246–259), cuando un solo `ROW_NUMBER()` resuelve lo mismo.
- **Delete + insert de dos meses completos cada día**: genera mucho registro de transacciones (*log*) y bloqueos. Como el histórico anterior no cambia, conviene cargar solo lo nuevo o cambiar particiones completas.
- `UPDATE` posterior de `diahabilnumero` (L407–412): se puede calcular directamente en el `INSERT` y ahorrar una pasada completa sobre la tabla.

### 3.4 Legibilidad y mantenibilidad
- **Sin comentarios** que expliquen las reglas de negocio (qué es "paz y salvo", "marcaespecialmeta" o "senda").
- Líneas de más de 300 caracteres, mayúsculas y minúsculas mezcladas, paréntesis innecesarios alrededor de columnas (`(cd.[credito]) as 'numerocredito'`) y alias entre comillas simples (estilo obsoleto).
- Lógica repetida: el `CASE` de `etapacobro` y el de `valcobrado` evalúan **las mismas 12 condiciones dos veces**, y `valcobrado` solo tiene dos resultados posibles. El mismo `CASE` de `rangototaldeuda` aparece dos veces (L165, L189).
- Nombres de columna con caracteres especiales (`'valcapitalini>30'`) que obligan a usar corchetes en todos lados.
- Un único script monolítico de 464 líneas que hace cinco procesos distintos: no se puede reejecutar solo una parte.

---

## 4. Mejoras propuestas

### 4.1 Estructura
1. **Separar en procedimientos almacenados**, uno por tabla destino (`usp_carga_asig_hist_dia`, `usp_carga_pagos`, …), orquestados por un SP maestro o un *job* de SQL Agent con pasos independientes y reintentos.
2. **Parámetro `@fecha_proceso DATE = NULL`** (por defecto la fecha de hoy). Todas las ventanas se calculan **una sola vez** a partir de él. Así se puede **reprocesar cualquier fecha** y las corridas son reproducibles.
3. **Tablas de parámetros y homologación** en lugar de valores fijos en el código: `param_producto` (código, nombre, excluir), `param_sucursal`, `param_rango` (tipo, desde, hasta, etiqueta) y `param_codigo_transaccion`. Los rangos se resuelven con un join `BETWEEN desde AND hasta`, sin `CASE` gigantes y sin solapes.

### 4.2 Integridad
4. Cada bloque dentro de `BEGIN TRY / BEGIN TRAN ... COMMIT / END TRY BEGIN CATCH ROLLBACK; THROW; END CATCH`: o se carga todo o no se borra nada.
5. **Bitácora** (`etl_log`: proceso, inicio, fin, filas, error) con `@@ROWCOUNT` después de cada paso.
6. Quitar `NOLOCK` y activar **`READ_COMMITTED_SNAPSHOT`** en la base: lecturas consistentes sin bloquear. Si se necesita aislamiento total de la carga del DW, usar `SNAPSHOT`.
7. **Listas de columnas explícitas** en todos los `INSERT`.
8. Corregir los errores de la sección 3.1: `PARTITION BY numerocredito` en `LAG`, la palabra `PREJUDICIAL`, `marcacionaliviofin`, `creditosdifsenda`, el corte de edad, `BIGINT` para la identificación y la ventana de pagos. En pagos, cargar el API **solo para lo que no venga del DW**: `NOT EXISTS` por código de transacción, o por crédito + fecha + valor.

### 4.3 Rendimiento
9. **Extraer primero, unir después**: traer de `DW_FZ_38` solo el rango necesario a `#stg` locales, con `OPENQUERY` o filtrando por `id_fecha >= @id_fecha_ini` para que el filtro se ejecute en el servidor remoto. Luego indexar y unir localmente. Resolver las 5 fechas con una sola `#dim_fecha` local (o calcularlas: `id_fecha` ya es `YYYYMMDD`).
10. **Filtros sargables**: `WHERE mesmetadiaria >= @mes_ini` sin `CAST`; `WHERE fecha_efect_movimiento >= @id_fecha_ini` sin join a `Dim_Fecha`.
11. **Fotos inicial y final con `ROW_NUMBER()`** (`OVER (PARTITION BY mes, credito ORDER BY fecha ASC/DESC)`) en una sola lectura, en lugar de `MIN/MAX` + auto-joins y de `MAX` columna por columna.
12. **Eliminar los `GROUP BY` de 100 columnas**: definir bien la granularidad de cada temporal (una fila por crédito-mes) y unir por su llave.
13. **Índices clustered** en las temporales: `CREATE CLUSTERED INDEX ... ON #x (mesmetadiaria, numerocredito)`.
14. Índices en destino: `asig_hist_dia (mesmetadiaria, numerocredito, fechametadiaria)` y `pagos (fecha_efect_movimiento)`. Para volúmenes grandes, **particionar por mes** y recargar con `TRUNCATE ... WITH (PARTITIONS)` + `SWITCH`, en vez de `DELETE` masivos que llenan el log.
15. Calcular `diahabilnumero` en el mismo `INSERT` con `SUM(diahabil) OVER (...)`, sin el `UPDATE` posterior.
16. Para el histórico que ya no cambia (meses cerrados), carga **incremental** con `MERGE` o insertar solo lo nuevo.

### 4.4 Legibilidad
17. Usar CTE con nombres que digan qué hacen (`cte_ultima_foto_mes`, `cte_paz_y_salvo`), comentarios de cabecera por bloque, alias `AS nombre` sin comillas, una columna por línea y nombres de columna sin caracteres especiales (`valcapitalini_mayor30`).

El **Anexo** muestra estas mejoras aplicadas a los bloques más críticos: ventanas de fecha, carga transaccional con bitácora, fotos inicial/final con `ROW_NUMBER`, `LAG` corregido, rangos por tabla de parámetros y recarga de pagos sin pérdida.

## Anexo – Propuesta de reestructuración (T-SQL)

Código de referencia con las mejoras de la sección 4 aplicadas a la foto diaria, el registro de paz y salvo, las fotos inicial y final con la transición de mora, y la carga de pagos. Las tablas origen y destino son las del script original.

```
/* =====================================================================
   Parte 3 – Propuesta de reestructuración de "consulta_original.sql"
   ---------------------------------------------------------------------
   Ilustra las mejoras sobre los bloques más críticos del script original.
   Las tablas origen/destino son las del script (no existen en este entorno),
   por lo que se presenta como diseño de referencia.

   Principios aplicados
     1. Un SP por tabla destino + un SP orquestador con @fecha_proceso.
     2. Ventanas de fecha calculadas UNA vez (sin CONCAT/LEFT/RIGHT).
     3. Extraer del linked server solo el rango necesario y unir en local.
     4. TRY/CATCH + transacción por bloque: si falla, no se pierde el periodo.
     5. Bitácora de cada paso con @@ROWCOUNT.
     6. Sin NOLOCK (READ_COMMITTED_SNAPSHOT en la BD) y con columnas explícitas.
     7. Reglas de negocio en tablas de parámetros, no en CASE fijos.
   ===================================================================== */
USE Cobranza;
GO

/* ---------- 0. Infraestructura: bitácora, parámetros, aislamiento ---------- */
-- Lecturas consistentes sin bloqueos (reemplaza los WITH (NOLOCK)):
-- ALTER DATABASE Cobranza SET READ_COMMITTED_SNAPSHOT ON WITH ROLLBACK IMMEDIATE;

IF OBJECT_ID('dbo.etl_log') IS NULL
CREATE TABLE dbo.etl_log (
    log_id        BIGINT IDENTITY PRIMARY KEY,
    proceso       SYSNAME        NOT NULL,
    paso          VARCHAR(100)   NOT NULL,
    fecha_proceso DATE           NOT NULL,
    inicio        DATETIME2(0)   NOT NULL DEFAULT SYSDATETIME(),
    filas         INT            NULL,
    error         NVARCHAR(4000) NULL
);
GO

-- Rangos parametrizados: reemplazan los ~15 CASE de rangos y evitan solapes/huecos
-- (rango semiabierto [desde, hasta) -> un valor cae siempre en un único tramo).
IF OBJECT_ID('dbo.param_rango') IS NULL
CREATE TABLE dbo.param_rango (
    tipo     VARCHAR(30)   NOT NULL,     -- 'MORA', 'CAPITAL', 'CUOTA', 'DESEMBOLSO', 'EDAD', ...
    desde    DECIMAL(18,2) NOT NULL,
    hasta    DECIMAL(18,2) NOT NULL,
    etiqueta VARCHAR(40)   NOT NULL,
    CONSTRAINT pk_param_rango PRIMARY KEY (tipo, desde),
    CONSTRAINT ck_param_rango CHECK (hasta > desde)
);
-- Ejemplo: INSERT dbo.param_rango VALUES ('EDAD',0,29,'A.19-28'),('EDAD',29,39,'B.29-38'),
--          ('EDAD',39,49,'C.39-48'),('EDAD',49,59,'D.49-58'),('EDAD',59,200,'E.>58');  -- 59 ya no cae en N/A

IF OBJECT_ID('dbo.param_producto') IS NULL
CREATE TABLE dbo.param_producto (
    tipo_producto INT         NOT NULL PRIMARY KEY,
    nombre        VARCHAR(40) NOT NULL,       -- '10.Vehic-FZ', '35.Titularizada', ...
    excluir       BIT         NOT NULL DEFAULT 0  -- 85 = Judic-EQ -> 1 (antes: DELETE fijo)
);

IF OBJECT_ID('dbo.param_senda') IS NULL
CREATE TABLE dbo.param_senda (
    estado_credito VARCHAR(30) NOT NULL PRIMARY KEY,   -- 'NORMAL','PREJUDICIAL','PREJURIDICO','CL. ESPECIAL',...
    senda          VARCHAR(30) NOT NULL                -- desconocidos -> 'Z.Sin homologar' (no 'Jurídico')
);
GO

/* ---------- 1. Foto diaria de cartera (bloque A + B del original) ---------- */
CREATE OR ALTER PROCEDURE dbo.usp_carga_asig_hist_dia
    @fecha_proceso DATE = NULL
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;      -- cualquier error revierte la transacción completa

    SET @fecha_proceso = ISNULL(@fecha_proceso, CAST(GETDATE() AS DATE));

    -- Ventana calculada una sola vez (antes: CONCAT/LEFT/RIGHT repetido ~20 veces)
    DECLARE @mes_ini     DATE = DATEADD(MONTH, DATEDIFF(MONTH, 0, @fecha_proceso) - 1, 0);  -- 1.er día mes anterior
    DECLARE @id_fecha_ini INT = CONVERT(INT, FORMAT(@mes_ini, 'yyyyMMdd'));
    DECLARE @filas INT;

    /* 1.1 Extraer del linked server SOLO el rango necesario (el filtro viaja al origen) */
    DROP TABLE IF EXISTS #cartera;
    SELECT cd.id_fecha, cd.id_fecha_desembolso, cd.id_fecha_vencimiento, cd.id_fecha_inicio_mora,
           cd.id_proxima_cuota, cd.credito, cd.tipo_id_deudor, cd.numero_id_deudor, cd.tipo_producto,
           cd.marcacion_alivio, cd.descripcion_marcacion_alivio, cd.codigo_sucursal,
           TRIM(cd.estado_obligacion) AS estado_obligacion, cd.valor_desembolso, cd.saldo_capital,
           cd.plazo_en_meses, cd.numero_dias_mora, cd.cuotas_pactadas, cd.cuotas_pagadas,
           cd.cuotas_pendientes, cd.cuotas_vencidas, cd.valor_cuota_capital_intereses, cd.valor_seguros,
           cd.valor_total_vencido, cd.Intereses_de_mora, cd.pago_minimo,
           cd.proxima_cuota_capital_intereses, cd.proxima_cuota_seguros, cd.proxima_cuota_total,
           cd.valor_vencido_mas_proxima_cuota, cd.valor_intereses, cd.cargos_1_2, cd.cargo_3,
           cd.sgr_pnd_pagar, cd.cargo_sancion, cd.valor_pago_ttl_canc, cd.valor_primera_cuota_vencida,
           cd.cdg_conces
    INTO #cartera
    FROM [DW_FZ_38].[DW_FZ].[dbo].[Fact_Cartera_Dia] AS cd
    WHERE cd.id_fecha >= @id_fecha_ini;                         -- sargable, sin CAST

    -- Las llaves de fecha son YYYYMMDD: se convierten sin 5 joins remotos a Dim_Fecha
    -- (los ceros/nulos se mapean a NULL en vez de perder la fila por INNER JOIN).
    CREATE CLUSTERED INDEX cx ON #cartera (credito, id_fecha);

    BEGIN TRY
        BEGIN TRAN;

        DELETE FROM dbo.asig_hist_dia
        WHERE mesmetadiaria >= @mes_ini;                         -- sargable

        INSERT INTO dbo.asig_hist_dia (
            anno, semestre, cuatrimestre, mes, nombremes, mesmetadiaria, fechametadiaria, dia, diaanno,
            semanaanno, nombrediasemana, diahabil, diahabilnumero, numerocredito, tipoiddeudor,
            numeroiddeudor, tipoproducto, marcacionalivio, tipoalivio, sucursal, estadocredito,
            valdesembolso, valcapital, fechadesembolso, fechavencimiento, fechainiciomora,
            fechaproximacuota, plazomeses, diasmora, cuotaspactadas, cuotaspagadas, cuotaspendientes,
            cuotasvencidas, valcuotacapitalinteres, valseguros, valtotalvencido, valinteresmora,
            pagominimo, valcuotacapitalinteresprox, valcuotasegurosprox, valcuotatotalprox,
            valvencidoprox, valinteres, valcargo1y2, valcargo3, valseguropendiente, cargosancion,
            valpagototalcancelado, valprimeracuotavencida, codigoconcesionario, fechaultactualizacion)
        SELECT f.Anno, f.Semestre, f.Cuatrimestre, f.Mes, f.MesNombre,
               DATEFROMPARTS(YEAR(x.fecha), MONTH(x.fecha), 1),          -- antes: CONCAT/LEFT/RIGHT
               x.fecha, f.Dia, f.DiaAnno, f.SemanaAnno, f.DiaNombre, CAST(f.DiaHabil AS INT),
               SUM(CAST(f.DiaHabil AS INT)) OVER (PARTITION BY c.credito, YEAR(x.fecha), MONTH(x.fecha)
                                                  ORDER BY x.fecha),     -- día hábil calculado aquí
               c.credito, c.tipo_id_deudor, c.numero_id_deudor, c.tipo_producto, c.marcacion_alivio,
               c.descripcion_marcacion_alivio, c.codigo_sucursal, c.estado_obligacion,
               c.valor_desembolso, c.saldo_capital,
               TRY_CONVERT(DATE, CONVERT(CHAR(8), c.id_fecha_desembolso)),
               TRY_CONVERT(DATE, CONVERT(CHAR(8), c.id_fecha_vencimiento)),
               TRY_CONVERT(DATE, CONVERT(CHAR(8), c.id_fecha_inicio_mora)),
               TRY_CONVERT(DATE, CONVERT(CHAR(8), c.id_proxima_cuota)),
               c.plazo_en_meses, c.numero_dias_mora, c.cuotas_pactadas, c.cuotas_pagadas,
               c.cuotas_pendientes, c.cuotas_vencidas, c.valor_cuota_capital_intereses, c.valor_seguros,
               c.valor_total_vencido, c.Intereses_de_mora, c.pago_minimo,
               c.proxima_cuota_capital_intereses, c.proxima_cuota_seguros, c.proxima_cuota_total,
               c.valor_vencido_mas_proxima_cuota, c.valor_intereses, c.cargos_1_2, c.cargo_3,
               c.sgr_pnd_pagar, c.cargo_sancion, c.valor_pago_ttl_canc, c.valor_primera_cuota_vencida,
               c.cdg_conces, SYSDATETIME()
        FROM #cartera AS c
        CROSS APPLY (SELECT CONVERT(DATE, CONVERT(CHAR(8), c.id_fecha)) AS fecha) AS x
        JOIN dbo.fecha AS f ON f.Fecha = x.fecha
        JOIN dbo.param_producto AS p ON p.tipo_producto = c.tipo_producto AND p.excluir = 0   -- antes: DELETE producto 85
        WHERE NOT EXISTS (SELECT 1 FROM dbo.fecha_medicion AS fm                                -- antes: DELETE posterior
                          WHERE fm.fechamedicion = x.fecha AND fm.marcaespecialmeta = 1);
        SET @filas = @@ROWCOUNT;
        INSERT dbo.etl_log (proceso, paso, fecha_proceso, filas)
        VALUES (OBJECT_NAME(@@PROCID), 'insert foto diaria', @fecha_proceso, @filas);

        /* 1.2 Registro "paz y salvo": créditos que salen de la cartera antes de fin de mes.
               Una sola lectura con ROW_NUMBER en lugar de 4 niveles de subconsultas + MAX. */
        ;WITH ult AS (
            SELECT h.*,
                   ROW_NUMBER() OVER (PARTITION BY h.mesmetadiaria, h.numerocredito
                                      ORDER BY h.fechametadiaria DESC) AS rn,
                   MAX(h.fechametadiaria) OVER (PARTITION BY h.mesmetadiaria) AS ult_foto_mes
            FROM dbo.asig_hist_dia AS h
            WHERE h.mesmetadiaria >= @mes_ini
        )
        INSERT INTO dbo.asig_hist_dia (
            anno, semestre, cuatrimestre, mes, nombremes, mesmetadiaria, fechametadiaria, dia, diaanno,
            semanaanno, nombrediasemana, diahabil, diahabilnumero, numerocredito, tipoiddeudor,
            numeroiddeudor, tipoproducto, marcacionalivio, tipoalivio, sucursal, estadocredito,
            valdesembolso, valcapital, fechadesembolso, fechavencimiento, fechainiciomora,
            fechaproximacuota, plazomeses, diasmora, cuotaspactadas, cuotaspagadas, cuotaspendientes,
            cuotasvencidas, valcuotacapitalinteres, valseguros, valtotalvencido, valinteresmora,
            pagominimo, valcuotacapitalinteresprox, valcuotasegurosprox, valcuotatotalprox,
            valvencidoprox, valinteres, valcargo1y2, valcargo3, valseguropendiente, cargosancion,
            valpagototalcancelado, valprimeracuotavencida, codigoconcesionario, fechaultactualizacion)
        SELECT f.Anno, f.Semestre, f.Cuatrimestre, f.Mes, f.MesNombre,
               DATEFROMPARTS(YEAR(d.fps), MONTH(d.fps), 1), d.fps, f.Dia, f.DiaAnno, f.SemanaAnno,
               f.DiaNombre, CAST(f.DiaHabil AS INT), 0, u.numerocredito, u.tipoiddeudor, u.numeroiddeudor,
               u.tipoproducto, u.marcacionalivio, u.tipoalivio, u.sucursal, u.estadocredito,
               u.valdesembolso, 0 /* capital en cero = paz y salvo */, u.fechadesembolso,
               u.fechavencimiento, u.fechainiciomora, u.fechaproximacuota, u.plazomeses, u.diasmora,
               u.cuotaspactadas, u.cuotaspagadas, u.cuotaspendientes, u.cuotasvencidas, 0, u.valseguros,
               u.valtotalvencido, u.valinteresmora, u.pagominimo, u.valcuotacapitalinteresprox,
               u.valcuotasegurosprox, u.valcuotatotalprox, u.valvencidoprox, u.valinteres,
               u.valcargo1y2, u.valcargo3, u.valseguropendiente, u.cargosancion,
               u.valpagototalcancelado, u.valprimeracuotavencida, u.codigoconcesionario, SYSDATETIME()
        FROM ult AS u
        CROSS APPLY (SELECT DATEADD(DAY, 1, u.fechametadiaria) AS fps) AS d
        JOIN dbo.fecha AS f ON f.Fecha = d.fps
        WHERE u.rn = 1
          AND u.fechametadiaria < u.ult_foto_mes      -- dejó de aparecer antes de la última foto del mes
          AND u.valcapital > 0
          AND d.fps <= EOMONTH(u.fechametadiaria);    -- EOMONTH en lugar de DATEADD(dd,-DAY(...))
        SET @filas = @@ROWCOUNT;
        INSERT dbo.etl_log (proceso, paso, fecha_proceso, filas)
        VALUES (OBJECT_NAME(@@PROCID), 'insert paz y salvo', @fecha_proceso, @filas);

        COMMIT;
    END TRY
    BEGIN CATCH
        IF @@TRANCOUNT > 0 ROLLBACK;                  -- el periodo NO queda borrado
        INSERT dbo.etl_log (proceso, paso, fecha_proceso, error)
        VALUES (OBJECT_NAME(@@PROCID), 'ERROR', @fecha_proceso, ERROR_MESSAGE());
        THROW;
    END CATCH
END
GO

/* ---------- 2. Fotos inicial / final / transición de mora (bloques D + F) ---------- */
-- Fragmento: reemplaza #asig_hist_dia_ini, #asig_hist_dia_fin (inexistente en el original),
-- #asig_hist_dia_max (MAX columna a columna) y el LAG sin PARTITION BY.
CREATE OR ALTER PROCEDURE dbo.usp_fotos_mes
    @mes_ini DATE
AS
BEGIN
    SET NOCOUNT ON;

    DROP TABLE IF EXISTS #fotos;
    SELECT h.mesmetadiaria, h.numerocredito, h.fechametadiaria, h.diasmora, h.valcapital,
           h.valtotalvencido, h.estadocredito, h.tipoproducto,
           -- tramo de mora parametrizado: [desde, hasta) sin solapes
           r.etiqueta AS rangomora,
           ROW_NUMBER() OVER (PARTITION BY h.mesmetadiaria, h.numerocredito ORDER BY h.fechametadiaria ASC)  AS rn_ini,
           ROW_NUMBER() OVER (PARTITION BY h.mesmetadiaria, h.numerocredito ORDER BY h.fechametadiaria DESC) AS rn_fin,
           ROW_NUMBER() OVER (PARTITION BY h.mesmetadiaria, h.numerocredito ORDER BY h.diasmora DESC, h.fechametadiaria DESC) AS rn_peor,
           -- LAG corregido: compara con el día anterior DEL MISMO crédito
           LAG(r.desde) OVER (PARTITION BY h.numerocredito ORDER BY h.fechametadiaria) AS tramo_anterior,
           r.desde AS tramo_actual
    INTO #fotos
    FROM dbo.asig_hist_dia AS h
    JOIN dbo.param_rango AS r
      ON r.tipo = 'MORA' AND h.diasmora >= r.desde AND h.diasmora < r.hasta
    WHERE h.mesmetadiaria >= @mes_ini;

    CREATE CLUSTERED INDEX cx ON #fotos (mesmetadiaria, numerocredito);

    -- Una fila por crédito-mes con foto inicial, final y "peor" foto COHERENTE (una fila real,
    -- no una mezcla de MAX por columna). Sin GROUP BY de 100+ columnas.
    SELECT i.mesmetadiaria, i.numerocredito,
           i.diasmora   AS diasmoraini,  fi.diasmora   AS diasmorafin,  pe.diasmora   AS diasmoradet,
           i.valcapital AS valcapitalini, fi.valcapital AS valcapitalfin, pe.valcapital AS valcapitaldet,
           i.rangomora  AS rangomoraini, fi.rangomora  AS rangomorafin,
           CASE WHEN fi.tramo_actual > i.tramo_actual THEN 'C.Deteriorado'
                WHEN fi.tramo_actual < i.tramo_actual THEN 'A.Mejorado'
                ELSE 'B.Permanecido' END AS estadomora
    FROM #fotos AS i
    JOIN #fotos AS fi ON fi.mesmetadiaria = i.mesmetadiaria AND fi.numerocredito = i.numerocredito AND fi.rn_fin = 1
    JOIN #fotos AS pe ON pe.mesmetadiaria = i.mesmetadiaria AND pe.numerocredito = i.numerocredito AND pe.rn_peor = 1
    WHERE i.rn_ini = 1;
END
GO

/* ---------- 3. Pagos sin pérdida de información (bloque G) ---------- */
CREATE OR ALTER PROCEDURE dbo.usp_carga_pagos
    @fecha_proceso DATE = NULL
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;
    SET @fecha_proceso = ISNULL(@fecha_proceso, CAST(GETDATE() AS DATE));

    -- Misma ventana que el resto del proceso (antes: DATEADD(mm,-1,GETDATE()) con hora)
    DECLARE @id_ini INT = CONVERT(INT, FORMAT(DATEADD(MONTH, DATEDIFF(MONTH, 0, @fecha_proceso) - 1, 0), 'yyyyMMdd'));
    DECLARE @id_api INT = CONVERT(INT, FORMAT(DATEADD(DAY, -5, @fecha_proceso), 'yyyyMMdd'));

    DROP TABLE IF EXISTS #recaudo;
    SELECT fpg.id_fecha_efe_mov, fpg.id_fecha_cal_mov, fpg.codigo_sucursal, fpg.credito, fpg.valor_total_pago,
           fpg.valor_pago_cargo_3, fpg.valor_pago_seguros, fpg.valor_pago_mor_segu, fpg.valor_pago_intereses_mora,
           fpg.valor_pago_int_cor, fpg.valor_pago_capital, fpg.sdo_int_c_desp_pago, fpg.saldo_favor_c,
           fpg.valor_pago_cargo_1, fpg.valor_pago_otros, fpg.cod_trans, fpg.ind_prepago, fpg.ind_trx_can,
           fpg.origen_pago
    INTO #recaudo
    FROM [DW_FZ_38].[DW_FZ].[dbo].[Fact_Recaudo] AS fpg
    WHERE fpg.id_fecha_efe_mov >= @id_ini;

    BEGIN TRY
        BEGIN TRAN;

        DELETE FROM dbo.pagos WHERE fecha_efect_movimiento >= @id_ini;   -- sin join remoto a Dim_Fecha

        INSERT INTO dbo.pagos (fecha, cod_sucursal, credito, fecha_cal_movimiento, fecha_efect_movimiento,
               valor_total_pago, valor_pago_cargo3, valor_pago_seguros, valor_pago_mora_seguros,
               valor_pago_interes_mora, valor_pago_interes_corriente, valor_pago_capital,
               saldo_interes_corriente_desp_pago, saldo_a_favor, valor_pago_cargo1, valor_pago_otros,
               codigo_transferencia, indicador_prepago, indicado_transaccion_can, origen_pago,
               fechaultactualizacion)
        SELECT CONVERT(DATE, CONVERT(CHAR(8), r.id_fecha_efe_mov)), r.codigo_sucursal, r.credito,
               r.id_fecha_cal_mov, r.id_fecha_efe_mov, r.valor_total_pago, r.valor_pago_cargo_3,
               r.valor_pago_seguros, r.valor_pago_mor_segu, r.valor_pago_intereses_mora,
               r.valor_pago_int_cor, r.valor_pago_capital, r.sdo_int_c_desp_pago, r.saldo_favor_c,
               r.valor_pago_cargo_1, r.valor_pago_otros, r.cod_trans, r.ind_prepago, r.ind_trx_can,
               r.origen_pago, SYSDATETIME()
        FROM #recaudo AS r;

        -- Pagos PSE del API: se AGREGAN solo los que aún no llegaron al DW
        -- (antes: se borraban TODOS los pagos de 5 días y se dejaban solo los del API).
        INSERT INTO dbo.pagos (fecha, cod_sucursal, credito, fecha_cal_movimiento, fecha_efect_movimiento,
               valor_total_pago, valor_pago_cargo3, valor_pago_seguros, valor_pago_mora_seguros,
               valor_pago_interes_mora, valor_pago_interes_corriente, valor_pago_capital,
               saldo_interes_corriente_desp_pago, saldo_a_favor, valor_pago_cargo1, valor_pago_otros,
               codigo_transferencia, indicador_prepago, indicado_transaccion_can, origen_pago,
               fechaultactualizacion)
        SELECT CONVERT(DATE, CONVERT(CHAR(8), pg.FechaEfectivaPago)), 114, pg.NroCredito,
               pg.FechaAplicacionPago, pg.FechaEfectivaPago, pg.ValorTotalAplicado, pg.ValorApropiadoCargo3,
               pg.ValorApropiadoSeguros, 0, pg.ValorApropiadoInteresMora, pg.ValorApropiadoInteresCorriente,
               pg.ValorApropiadoACapital, 0, 0, pg.ValorApropiadoCargo1, 0, pg.CodigoTransaccion, 0, 0,
               '28F PAG PSE 281', SYSDATETIME()
        FROM [DW_FZ_38].[DW_Apis].[SG].[SignatureData] AS pg
        WHERE pg.FechaEfectivaPago >= @id_api
          AND pg.CodigoTransaccion IN (SELECT codigo FROM dbo.param_codigo_transaccion WHERE es_pse = 1)
          AND NOT EXISTS (SELECT 1 FROM #recaudo AS r
                          WHERE r.credito = pg.NroCredito
                            AND r.id_fecha_efe_mov = pg.FechaEfectivaPago
                            AND r.valor_total_pago = pg.ValorTotalAplicado);

        COMMIT;
    END TRY
    BEGIN CATCH
        IF @@TRANCOUNT > 0 ROLLBACK;
        INSERT dbo.etl_log (proceso, paso, fecha_proceso, error)
        VALUES (OBJECT_NAME(@@PROCID), 'ERROR', @fecha_proceso, ERROR_MESSAGE());
        THROW;
    END CATCH
END
GO

/* ---------- 4. Orquestador (job diario de SQL Agent) ---------- */
CREATE OR ALTER PROCEDURE dbo.usp_proceso_cobranza_diario
    @fecha_proceso DATE = NULL
AS
BEGIN
    SET NOCOUNT ON;
    SET @fecha_proceso = ISNULL(@fecha_proceso, CAST(GETDATE() AS DATE));   -- una sola "fecha de corte"
    EXEC dbo.usp_carga_asig_hist_dia @fecha_proceso;
    EXEC dbo.usp_carga_pagos         @fecha_proceso;
    -- EXEC dbo.usp_carga_asig_hist_dia_agrup @fecha_proceso;
    -- EXEC dbo.usp_carga_pago_hist_dia      @fecha_proceso;
    -- EXEC dbo.usp_carga_asig_hist_mes      @fecha_proceso;
END
GO

/* ---------- 5. Correcciones puntuales de reglas (sobre el código original) ----------
   L91   'PREJUDICAL'  -> homologar en dbo.param_senda ('PREJUDICIAL' -> 'B.Pre-Juridico');
                          ELSE -> 'Z.Sin homologar' (no 'C.Jurídico').
   L84   CAST(numeroiddeudor AS int) -> CAST(numeroiddeudor AS bigint)  (evita desbordamiento).
   L426  WHEN ah.marcacionalivioini = 88 -> WHEN ah.marcacionaliviofin = 88.
   L429  creditosdifsenda: CASE WHEN tiposendaclienteini = tiposendaclientefin THEN 0 ELSE 1 END.
   L233  Edad 59 -> rango 'E.>58' (via param_rango semiabierto).
   L127-140 Rangos de antigüedad contra la fecha de la foto, no GETDATE():
            DATEDIFF(MONTH, fechadesembolso, fechametadiaria).
   L109-122 valcobrado (12 ramas con solo 2 resultados posibles) se reduce a:
            CASE WHEN diasmora <= 0 AND valtotalvencido <= 0
                      AND (fechaproximacuota >= DATEFROMPARTS(YEAR(fechametadiaria), MONTH(fechametadiaria), 1)
                           OR fechaproximacuota = '19000101')
                 THEN valcuotacapitalinteresprox + valcuotasegurosprox   -- cobro preventivo
                 ELSE valtotalvencido + valinteresmora END               -- cobro administrativo
   ------------------------------------------------------------------------------------- */
```
