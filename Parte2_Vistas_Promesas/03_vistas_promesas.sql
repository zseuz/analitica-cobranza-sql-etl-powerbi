/* =====================================================================
   Parte 2: vistas de seguimiento a promesas de pago
   ---------------------------------------------------------------------
   ¡IMPORTANTE! MODO DEMOSTRACIÓN
   Los datos terminan en agosto de 2025. Para que las vistas
   muestren resultados, este script deja "hoy" fijo en 2025-07-25 en la
   tabla seg.parametro. Con esa fecha fija el filtro NO cambia de un día a otro.

   Para operación real (el filtro se restablece solo cada día con GETDATE()):
       UPDATE seg.parametro SET fecha_referencia = NULL WHERE id = 1;
   Para simular otro día:
       UPDATE seg.parametro SET fecha_referencia = '2025-07-24' WHERE id = 1;
   ---------------------------------------------------------------------
   Combina la Base 3 (Comité: mora, estado, cuota) y la Base 4 (Gestiones:
   promesas) para generar dos vistas EXCLUYENTES:

     seg.vw_promesas_incumplidas_3d   Vista 1 – promesas incumplidas de los
                                      últimos 3 días (hoy, ayer y antier)
     seg.vw_mora60_incumplidas_hoy    Vista 2 – créditos con más de 60 días de
                                      mora y promesa incumplida del mismo día

   Objetos
     seg.parametro                    fecha de referencia (NULL = hoy)
     seg.fn_promesas_incumplidas      lógica única (CTE) que usan ambas vistas
     seg.vw_...                       las dos vistas solicitadas
     seg.usp_seguimiento_promesas     proceso diario con variables + tablas
                                      temporales + CTE; guarda historial
     seg.historial_seguimiento        foto de cada ejecución del proceso

   Reglas de negocio
   * Promesa: gestión con fecha prometida (Base 4).
   * Incumplida: entre el día de la gestión y el día prometido no hubo recaudo
     EFECTIVO (Base 2, sin Ajuste/Otros) que cubra al menos el 90 % de lo prometido
     (o ningún pago si la promesa no trae valor). La Base 2 se usa solo para
     verificar el pago: sin ella toda promesa vencida contaría como incumplida.
   * Si un crédito tiene varias promesas para el mismo día, es una sola fila
     (incumplida solo si ninguna de ellas se cumplió).
   * Mora, estado y cuota: foto de Comité del mes de referencia o la anterior
     más reciente (si no existe, la más próxima).
   * Celular: último celular conocido del crédito (Reporte1).
   * Mora +60 (Vista 2): días de mora > 60 y distinto de 999. El 999 es un valor
     centinela de Comité ("sin dato"): no permite afirmar que el crédito tenga
     más de 60 días de mora, así que esos créditos se reportan en la Vista 1.
   * Exclusión mutua: si un crédito cumple la Vista 2 se reporta SOLO ahí (es el
     caso más crítico) y se excluye completo de la Vista 1.
   * Reinicio diario: con seg.parametro.fecha_referencia = NULL las vistas
     calculan "hoy" con GETDATE() en cada consulta, así que el filtro se
     restablece solo cada día, sin jobs ni tablas intermedias (ver MODO DEMOSTRACIÓN).
   ===================================================================== */
USE AnaliticaCobranza;
GO
IF SCHEMA_ID('seg') IS NULL EXEC('CREATE SCHEMA seg');
GO

/* ---------------------------------------------------------------------
   Fecha de referencia
   NULL  -> se usa la fecha del servidor (operación diaria real)
   fecha -> se "simula" ese día (los datos son de jun-ago 2025)
   Se crea con 2025-07-25 para la demostración (ver aviso al inicio del script).
   Si la tabla ya existe no se toca: así no se pisa el valor de producción.
   --------------------------------------------------------------------- */
IF OBJECT_ID('seg.parametro') IS NULL
BEGIN
    CREATE TABLE seg.parametro (
        id               TINYINT      NOT NULL CONSTRAINT pk_seg_parametro PRIMARY KEY
                                      CONSTRAINT ck_seg_parametro_unico CHECK (id = 1),   -- una sola fila
        fecha_referencia DATE         NULL,
        descripcion      VARCHAR(300) NOT NULL
    );
    INSERT seg.parametro VALUES
    (1, '2025-07-25', 'MODO DEMOSTRACIÓN: fecha que las vistas consideran "hoy". Poner NULL en producción para usar GETDATE().');
END
GO

/* ---------------------------------------------------------------------
   Lógica común: promesas incumplidas con fecha prometida en los últimos
   3 días respecto a @fecha_ref (función en línea = se expande como una vista
   parametrizada, sin costo extra)
   --------------------------------------------------------------------- */
CREATE OR ALTER FUNCTION seg.fn_promesas_incumplidas (@fecha_ref DATE)
RETURNS TABLE
AS
RETURN
WITH ventana AS (           -- límites de la ventana como llaves enteras YYYYMMDD, para filtrar
                            -- directamente sobre los índices de fecha de dw.fact_gestion
    SELECT YEAR(DATEADD(DAY, -2, @fecha_ref)) * 10000 + MONTH(DATEADD(DAY, -2, @fecha_ref)) * 100
             + DAY(DATEADD(DAY, -2, @fecha_ref))                                   AS id_desde,
           YEAR(@fecha_ref) * 10000 + MONTH(@fecha_ref) * 100 + DAY(@fecha_ref)    AS id_hasta,
           YEAR(@fecha_ref) * 12 + MONTH(@fecha_ref)                       AS mes_ref
),
promesas AS (               -- Base 4: promesas con fecha prometida dentro de la ventana
    SELECT g.gestion_id, g.credito_id, g.fecha_id AS fecha_gestion_id,
           g.fecha_promesa_id, g.valor_promesa
    FROM dw.fact_gestion AS g
    CROSS JOIN ventana AS v
    WHERE g.fecha_promesa_id BETWEEN v.id_desde AND v.id_hasta
),
evaluadas AS (              -- pago efectivo entre la gestión y el día prometido; cumplida si
                            -- cubre el 90 % de lo prometido (mismo criterio del data mart)
    SELECT p.*, ISNULL(pg.valor_pagado, 0) AS valor_pagado,
           CASE WHEN (p.valor_promesa > 0 AND ISNULL(pg.valor_pagado, 0) >= p.valor_promesa * 0.90)
                  OR (p.valor_promesa = 0 AND ISNULL(pg.valor_pagado, 0) > 0)
                THEN 1 ELSE 0 END AS es_cumplida
    FROM promesas AS p
    OUTER APPLY (SELECT SUM(r.valor_total_pago) AS valor_pagado
                 FROM dw.fact_recaudo AS r
                 JOIN dw.dim_origen_pago AS o ON o.origen_pago_id = r.origen_pago_id
                 WHERE r.credito_id = p.credito_id
                   AND o.es_recaudo_efectivo = 1
                   AND r.fecha_id BETWEEN p.fecha_gestion_id AND p.fecha_promesa_id) AS pg
),
incumplidas AS (            -- 1 fila por crédito y día prometido
    SELECT credito_id, fecha_promesa_id,
           MAX(valor_promesa) AS valor_promesa, MAX(valor_pagado) AS valor_pagado,
           COUNT(*) AS gestiones_promesa
    FROM evaluadas
    GROUP BY credito_id, fecha_promesa_id
    HAVING MAX(es_cumplida) = 0
)
SELECT i.credito_id,
       cel.celular,
       foto.valor_cuota_estimada AS valor_cuota,
       foto.dias_mora,
       -- mora +60 válida: 999 es el valor centinela "sin dato" de Comité, no 999 días reales
       CASE WHEN foto.dias_mora > 60 AND foto.dias_mora < 999 THEN 1 ELSE 0 END AS es_mora_mas_60,
       foto.estado_cartera       AS estado_credito,
       f.fecha                   AS dia_promesa,
       i.valor_promesa, i.valor_pagado, i.gestiones_promesa,
       foto.periodo              AS periodo_foto_cartera
FROM incumplidas AS i
CROSS JOIN ventana AS v
JOIN dw.dim_fecha AS f ON f.fecha_id = i.fecha_promesa_id
OUTER APPLY (               -- Base 3: foto del mes de referencia o la anterior más reciente
    SELECT TOP 1 c.periodo, c.dias_mora, c.estado_cartera, c.valor_cuota_estimada
    FROM dw.fact_cartera_mensual AS c
    WHERE c.credito_id = i.credito_id
    ORDER BY CASE WHEN (c.periodo / 100) * 12 + c.periodo % 100 <= v.mes_ref THEN 0 ELSE 1 END,
             ABS((c.periodo / 100) * 12 + c.periodo % 100 - v.mes_ref)) AS foto
OUTER APPLY (               -- último celular conocido (Reporte1)
    SELECT TOP 1 s.celular FROM dw.fact_sms AS s
    WHERE s.credito_id = i.credito_id
    ORDER BY s.fecha_envio DESC, s.sms_id DESC) AS cel;
GO

/* ---------------------------------------------------------------------
   VISTA 2 – Créditos con más de 60 días de mora y promesa incumplida HOY
   --------------------------------------------------------------------- */
CREATE OR ALTER VIEW seg.vw_mora60_incumplidas_hoy
AS
WITH ref AS (
    SELECT COALESCE((SELECT fecha_referencia FROM seg.parametro WHERE id = 1),
                    CAST(GETDATE() AS DATE)) AS hoy
)
SELECT i.credito_id      AS credito,
       i.celular         AS telefono_celular,
       i.valor_cuota,
       i.dias_mora       AS numero_dias_mora,
       i.estado_credito,
       i.dia_promesa,
       GETDATE()         AS fecha_hora_ejecucion
FROM ref
CROSS APPLY seg.fn_promesas_incumplidas(ref.hoy) AS i
WHERE i.es_mora_mas_60 = 1
  AND i.dia_promesa = ref.hoy;
GO

/* ---------------------------------------------------------------------
   VISTA 1 – Promesas incumplidas de los últimos 3 días (incluye hoy)
   Excluye cualquier crédito que ya esté en la Vista 2.
   --------------------------------------------------------------------- */
CREATE OR ALTER VIEW seg.vw_promesas_incumplidas_3d
AS
WITH ref AS (
    SELECT COALESCE((SELECT fecha_referencia FROM seg.parametro WHERE id = 1),
                    CAST(GETDATE() AS DATE)) AS hoy
),
incumplidas AS (
    SELECT i.*,
           -- ¿el crédito cumple la condición de la Vista 2? (se evalúa una sola vez, sin
           -- volver a llamar la función: MAX sobre la partición del crédito)
           MAX(CASE WHEN i.es_mora_mas_60 = 1 AND i.dia_promesa = ref.hoy THEN 1 ELSE 0 END)
               OVER (PARTITION BY i.credito_id) AS esta_en_vista2
    FROM ref
    CROSS APPLY seg.fn_promesas_incumplidas(ref.hoy) AS i
)
SELECT i.credito_id      AS credito,
       i.celular         AS telefono_celular,
       i.valor_cuota,
       i.dias_mora       AS numero_dias_mora,
       i.estado_credito,
       i.dia_promesa,
       GETDATE()         AS fecha_hora_ejecucion
FROM incumplidas AS i
WHERE i.esta_en_vista2 = 0;          -- exclusión mutua con la Vista 2
GO

/* ---------------------------------------------------------------------
   Proceso diario (opcional para un job de SQL Agent): misma lógica con
   variables, tablas temporales y CTE; guarda la foto de cada ejecución.
   --------------------------------------------------------------------- */
IF OBJECT_ID('seg.historial_seguimiento') IS NULL
CREATE TABLE seg.historial_seguimiento (
    historial_id         BIGINT IDENTITY CONSTRAINT pk_seg_historial PRIMARY KEY,
    fecha_hora_ejecucion DATETIME2(0)  NOT NULL,
    fecha_referencia     DATE          NOT NULL,
    vista                TINYINT       NOT NULL CONSTRAINT ck_seg_historial_vista CHECK (vista IN (1, 2)),
    credito              BIGINT        NOT NULL,
    telefono_celular     VARCHAR(10)   NULL,
    valor_cuota          DECIMAL(18,2) NULL,
    numero_dias_mora     SMALLINT      NULL,
    estado_credito       VARCHAR(30)   NULL,
    dia_promesa          DATE          NOT NULL
);
GO

CREATE OR ALTER PROCEDURE seg.usp_seguimiento_promesas
    @fecha_referencia DATE = NULL,     -- NULL -> parámetro de seg.parametro -> hoy
    @guardar_historial BIT = 1
AS
BEGIN
    SET NOCOUNT ON;

    DECLARE @hoy DATE = COALESCE(@fecha_referencia,
                                 (SELECT fecha_referencia FROM seg.parametro WHERE id = 1),
                                 CAST(GETDATE() AS DATE));
    DECLARE @ejecucion DATETIME2(0) = SYSDATETIME();

    -- 1) Promesas incumplidas de la ventana (lógica común)
    DROP TABLE IF EXISTS #incumplidas;
    SELECT * INTO #incumplidas FROM seg.fn_promesas_incumplidas(@hoy);

    -- 2) Clasificación excluyente: Vista 2 tiene prioridad sobre la Vista 1
    DROP TABLE IF EXISTS #resultado;
    WITH vista2 AS (
        SELECT DISTINCT credito_id FROM #incumplidas WHERE es_mora_mas_60 = 1 AND dia_promesa = @hoy
    )
    SELECT CASE WHEN v2.credito_id IS NULL THEN 1 ELSE 2 END AS vista, i.*
    INTO #resultado
    FROM #incumplidas AS i
    LEFT JOIN vista2 AS v2 ON v2.credito_id = i.credito_id
    WHERE v2.credito_id IS NULL                                   -- Vista 1: créditos fuera de la Vista 2
       OR (i.es_mora_mas_60 = 1 AND i.dia_promesa = @hoy);        -- Vista 2: solo la promesa de hoy

    IF @guardar_historial = 1
        INSERT seg.historial_seguimiento (fecha_hora_ejecucion, fecha_referencia, vista, credito,
               telefono_celular, valor_cuota, numero_dias_mora, estado_credito, dia_promesa)
        SELECT @ejecucion, @hoy, vista, credito_id, celular, valor_cuota, dias_mora, estado_credito, dia_promesa
        FROM #resultado;

    -- 3) Resultados: Vista 1 y Vista 2
    SELECT credito_id AS credito, celular AS telefono_celular, valor_cuota, dias_mora AS numero_dias_mora,
           estado_credito, dia_promesa, @ejecucion AS fecha_hora_ejecucion
    FROM #resultado WHERE vista = 1 ORDER BY dia_promesa DESC, dias_mora DESC;

    SELECT credito_id AS credito, celular AS telefono_celular, valor_cuota, dias_mora AS numero_dias_mora,
           estado_credito, dia_promesa, @ejecucion AS fecha_hora_ejecucion
    FROM #resultado WHERE vista = 2 ORDER BY dias_mora DESC;
END
GO
