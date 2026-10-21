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
