/* =====================================================================
   Parte 1, punto 1: modelo relacional
   ---------------------------------------------------------------------
   Base de datos : AnaliticaCobranza
   Esquemas      : stg  -> aterrizaje crudo (texto) del Reporte1 diario
                   dw   -> modelo relacional limpio (dimensiones + hechos)
                   etl  -> control de archivos, rechazos y bitácora
                   mart -> tablas finales para Power BI (ver 02_tablas_data_mart.sql)
   El script es idempotente: se puede ejecutar varias veces.
   ===================================================================== */
IF DB_ID('AnaliticaCobranza') IS NULL
    CREATE DATABASE AnaliticaCobranza;
GO
USE AnaliticaCobranza;
GO
IF SCHEMA_ID('stg')  IS NULL EXEC('CREATE SCHEMA stg');
IF SCHEMA_ID('dw')   IS NULL EXEC('CREATE SCHEMA dw');
IF SCHEMA_ID('etl')  IS NULL EXEC('CREATE SCHEMA etl');
IF SCHEMA_ID('mart') IS NULL EXEC('CREATE SCHEMA mart');
GO

/* ---------------------------------------------------------------------
   DIMENSIONES
   --------------------------------------------------------------------- */

-- Calendario 2020-2030 (llave entera YYYYMMDD, igual al formato de las fuentes)
IF OBJECT_ID('dw.dim_fecha') IS NULL
BEGIN
    CREATE TABLE dw.dim_fecha (
        fecha_id      INT          NOT NULL CONSTRAINT pk_dim_fecha PRIMARY KEY,  -- YYYYMMDD
        fecha         DATE         NOT NULL CONSTRAINT uq_dim_fecha UNIQUE,
        anio          SMALLINT     NOT NULL,
        mes           TINYINT      NOT NULL,
        nombre_mes    VARCHAR(12)  NOT NULL,
        periodo       INT          NOT NULL,   -- YYYYMM (llave de cartera mensual)
        dia           TINYINT      NOT NULL,
        dia_semana    TINYINT      NOT NULL,   -- 1 = lunes
        nombre_dia    VARCHAR(10)  NOT NULL,
        es_fin_semana BIT          NOT NULL
    );

    ;WITH n AS (SELECT TOP (DATEDIFF(DAY,'2020-01-01','2030-12-31') + 1)
                       ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) - 1 AS k
                FROM sys.all_objects a CROSS JOIN sys.all_objects b),
          d AS (SELECT DATEADD(DAY, k, CAST('2020-01-01' AS DATE)) AS f FROM n)
    INSERT dw.dim_fecha
    SELECT CONVERT(INT, FORMAT(f,'yyyyMMdd')), f, YEAR(f), MONTH(f),
           CHOOSE(MONTH(f),'Enero','Febrero','Marzo','Abril','Mayo','Junio','Julio',
                  'Agosto','Septiembre','Octubre','Noviembre','Diciembre'),
           YEAR(f)*100 + MONTH(f), DAY(f),
           (DATEDIFF(DAY,'19000101',f) % 7) + 1,
           CHOOSE((DATEDIFF(DAY,'19000101',f) % 7) + 1,'Lunes','Martes','Miércoles','Jueves','Viernes','Sábado','Domingo'),
           CASE WHEN (DATEDIFF(DAY,'19000101',f) % 7) >= 5 THEN 1 ELSE 0 END
    FROM d;
END
GO

-- Crédito: llave numérica normalizada (las fuentes traen prefijos/sufijos de texto:
-- 'OP-1685153-A', 'C_1135300_OK', 'CTO1323881X' ... -> 1685153, 1135300, 1323881)
IF OBJECT_ID('dw.dim_credito') IS NULL
CREATE TABLE dw.dim_credito (
    credito_id       BIGINT       NOT NULL CONSTRAINT pk_dim_credito PRIMARY KEY,
    fuente_alta      VARCHAR(20)  NOT NULL,              -- primera base donde se vio
    fecha_alta_dw    DATETIME2(0) NOT NULL CONSTRAINT df_dim_credito_alta DEFAULT SYSDATETIME()
);
GO

-- Tipificación de gestiones (descripcion_perfil_historico estandarizada + reglas de negocio)
IF OBJECT_ID('dw.dim_tipificacion') IS NULL
CREATE TABLE dw.dim_tipificacion (
    tipificacion_id      SMALLINT IDENTITY CONSTRAINT pk_dim_tipificacion PRIMARY KEY,
    tipificacion         VARCHAR(80) NOT NULL CONSTRAINT uq_dim_tipificacion UNIQUE,
    canal                VARCHAR(30) NOT NULL,   -- Telefónica / Mensajería / Notificación / Administrativa
    categoria            VARCHAR(30) NOT NULL,   -- Contacto directo / Contacto indirecto / No contacto / ...
    es_intento_contacto  BIT NOT NULL,           -- cuenta en el denominador de contactabilidad
    es_contacto          BIT NOT NULL,           -- se habló con alguien (titular o tercero)
    es_contacto_directo  BIT NOT NULL,           -- se habló con el titular
    es_compromiso        BIT NOT NULL            -- tipificación propia de promesa/acuerdo
);
GO

-- Origen del pago estandarizado; es_recaudo_efectivo = 0 para Ajuste / Otros / Otros sin información
IF OBJECT_ID('dw.dim_origen_pago') IS NULL
CREATE TABLE dw.dim_origen_pago (
    origen_pago_id      SMALLINT IDENTITY CONSTRAINT pk_dim_origen_pago PRIMARY KEY,
    origen_pago         VARCHAR(40) NOT NULL CONSTRAINT uq_dim_origen_pago UNIQUE,
    es_recaudo_efectivo BIT NOT NULL,
    es_reversion        BIT NOT NULL
);
GO

-- Plantillas de SMS (el texto se repite en miles de filas: se guarda una sola vez)
IF OBJECT_ID('dw.dim_plantilla_sms') IS NULL
CREATE TABLE dw.dim_plantilla_sms (
    plantilla_id   INT IDENTITY CONSTRAINT pk_dim_plantilla_sms PRIMARY KEY,
    hash_mensaje   BINARY(32)     NOT NULL CONSTRAINT uq_dim_plantilla_sms UNIQUE,
    mensaje        NVARCHAR(1000) NOT NULL
);
GO

/* ---------------------------------------------------------------------
   HECHOS (bases 2, 3 y 4: carga completa idempotente)
   --------------------------------------------------------------------- */

-- Base 3 – Comité: foto de cierre de mes por crédito
IF OBJECT_ID('dw.fact_cartera_mensual') IS NULL
CREATE TABLE dw.fact_cartera_mensual (
    credito_id          BIGINT        NOT NULL CONSTRAINT fk_cartera_credito REFERENCES dw.dim_credito(credito_id),
    periodo             INT           NOT NULL,             -- YYYYMM
    codigo_origen       VARCHAR(30)   NOT NULL,             -- ID tal como venía (trazabilidad)
    tipo_producto       SMALLINT      NOT NULL,
    saldo_capital       DECIMAL(18,2) NOT NULL CONSTRAINT ck_cartera_saldo CHECK (saldo_capital >= 0),
    tasa_nominal        DECIMAL(9,4)  NULL,                 -- % mes vencido (0 -> NULL)
    tasa_actual         DECIMAL(9,4)  NULL,                 -- % efectivo anual
    cuotas_pagadas      SMALLINT      NULL CONSTRAINT ck_cartera_cuotas CHECK (cuotas_pagadas >= 0),
    plazo_total         SMALLINT      NOT NULL CONSTRAINT ck_cartera_plazo CHECK (plazo_total > 0),
    cuotas_pendientes   AS (CASE WHEN plazo_total - ISNULL(cuotas_pagadas,0) > 0
                                 THEN plazo_total - ISNULL(cuotas_pagadas,0) ELSE 1 END) PERSISTED,
    valor_cuota_estimada DECIMAL(18,2) NULL,                -- cuota fija (anualidad) estimada en el ETL
    estado_cartera      VARCHAR(30)   NOT NULL,
    dias_mora           SMALLINT      NOT NULL CONSTRAINT ck_cartera_mora CHECK (dias_mora BETWEEN 0 AND 999),
    mora_centinela      AS (CASE WHEN dias_mora = 999 THEN 1 ELSE 0 END) PERSISTED, -- 999 = valor centinela de la fuente
    tramo_mora          AS (CASE WHEN dias_mora = 0   THEN '0. Al día'
                                 WHEN dias_mora <= 30  THEN '1. 1-30'
                                 WHEN dias_mora <= 60  THEN '2. 31-60'
                                 WHEN dias_mora <= 90  THEN '3. 61-90'
                                 WHEN dias_mora <= 180 THEN '4. 91-180'
                                 WHEN dias_mora <= 360 THEN '5. 181-360'
                                 WHEN dias_mora < 999  THEN '6. +360'
                                 ELSE '7. Sin dato (999)' END) PERSISTED,
    CONSTRAINT pk_fact_cartera_mensual PRIMARY KEY (credito_id, periodo)
);
GO

-- Base 2 – Recaudo
IF OBJECT_ID('dw.fact_recaudo') IS NULL
CREATE TABLE dw.fact_recaudo (
    recaudo_id               BIGINT IDENTITY CONSTRAINT pk_fact_recaudo PRIMARY KEY,
    credito_id               BIGINT        NOT NULL CONSTRAINT fk_recaudo_credito REFERENCES dw.dim_credito(credito_id),
    fecha_id                 INT           NOT NULL CONSTRAINT fk_recaudo_fecha   REFERENCES dw.dim_fecha(fecha_id),
    origen_pago_id           SMALLINT      NOT NULL CONSTRAINT fk_recaudo_origen  REFERENCES dw.dim_origen_pago(origen_pago_id),
    codigo_origen            VARCHAR(30)   NOT NULL,
    valor_total_pago         DECIMAL(18,2) NOT NULL,   -- negativo en reversiones
    valor_pago_intereses_mora DECIMAL(18,2) NOT NULL,
    valor_pago_capital       DECIMAL(18,2) NOT NULL
);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_recaudo_credito_fecha')
    CREATE INDEX ix_recaudo_credito_fecha ON dw.fact_recaudo (credito_id, fecha_id) INCLUDE (valor_total_pago, origen_pago_id);
GO

-- Base 4 – Gestiones de cobranza (incluye promesas)
IF OBJECT_ID('dw.fact_gestion') IS NULL
CREATE TABLE dw.fact_gestion (
    gestion_id        BIGINT IDENTITY CONSTRAINT pk_fact_gestion PRIMARY KEY,
    credito_id        BIGINT        NOT NULL CONSTRAINT fk_gestion_credito REFERENCES dw.dim_credito(credito_id),
    fecha_id          INT           NOT NULL CONSTRAINT fk_gestion_fecha   REFERENCES dw.dim_fecha(fecha_id),
    fecha_hora        DATETIME2(0)  NOT NULL,
    tipificacion_id   SMALLINT      NOT NULL CONSTRAINT fk_gestion_tipif   REFERENCES dw.dim_tipificacion(tipificacion_id),
    codigo_origen     VARCHAR(30)   NOT NULL,
    fecha_promesa_id  INT           NULL CONSTRAINT fk_gestion_fecha_prom REFERENCES dw.dim_fecha(fecha_id),
    valor_promesa     DECIMAL(18,2) NOT NULL CONSTRAINT df_gestion_valor_prom DEFAULT 0 CONSTRAINT ck_gestion_valor_prom CHECK (valor_promesa >= 0),
    es_promesa        AS (CASE WHEN fecha_promesa_id IS NOT NULL THEN 1 ELSE 0 END) PERSISTED,
    CONSTRAINT ck_gestion_promesa_futura CHECK (fecha_promesa_id IS NULL OR fecha_promesa_id >= fecha_id)
);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_gestion_credito_fecha')
    CREATE INDEX ix_gestion_credito_fecha ON dw.fact_gestion (credito_id, fecha_id);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_gestion_promesa')
    CREATE INDEX ix_gestion_promesa ON dw.fact_gestion (fecha_promesa_id) WHERE fecha_promesa_id IS NOT NULL;
GO

/* ---------------------------------------------------------------------
   CONTROL DEL ETL (Reporte1 diario)
   --------------------------------------------------------------------- */
IF OBJECT_ID('etl.control_archivo') IS NULL
CREATE TABLE etl.control_archivo (
    archivo_id        INT IDENTITY CONSTRAINT pk_control_archivo PRIMARY KEY,
    fuente            VARCHAR(30)    NOT NULL,
    nombre_archivo    NVARCHAR(260)  NOT NULL,
    hash_sha256       CHAR(64)       NOT NULL,
    inicio            DATETIME2(0)   NOT NULL CONSTRAINT df_control_inicio DEFAULT SYSDATETIME(),
    fin               DATETIME2(0)   NULL,
    estado            VARCHAR(15)    NOT NULL CONSTRAINT ck_control_estado CHECK (estado IN ('EN_PROCESO','OK','ERROR','OMITIDO')),
    filas_leidas      INT NULL,
    filas_rechazadas  INT NULL,
    filas_duplicadas  INT NULL,      -- repetidas dentro del archivo o ya cargadas antes
    filas_insertadas  INT NULL,
    mensaje           NVARCHAR(4000) NULL
);
GO
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ux_control_archivo_ok')
    -- un mismo archivo (mismo contenido) solo puede quedar cargado OK una vez
    CREATE UNIQUE INDEX ux_control_archivo_ok ON etl.control_archivo (fuente, hash_sha256) WHERE estado = 'OK';
GO

IF OBJECT_ID('etl.rechazo') IS NULL
CREATE TABLE etl.rechazo (
    rechazo_id   BIGINT IDENTITY CONSTRAINT pk_rechazo PRIMARY KEY,
    archivo_id   INT            NULL CONSTRAINT fk_rechazo_archivo REFERENCES etl.control_archivo(archivo_id),
    fuente       VARCHAR(30)    NOT NULL,
    fila_origen  INT            NULL,
    motivo       VARCHAR(200)   NOT NULL,
    registro     NVARCHAR(MAX)  NOT NULL,    -- fila original en JSON
    fecha        DATETIME2(0)   NOT NULL CONSTRAINT df_rechazo_fecha DEFAULT SYSDATETIME()
);
GO

/* ---------------------------------------------------------------------
   Base 1 – Reporte1 (SMS) : staging + hecho incremental
   --------------------------------------------------------------------- */
IF OBJECT_ID('stg.reporte1') IS NULL
CREATE TABLE stg.reporte1 (            -- ya limpio por Python; se trunca en cada corrida
    archivo_id         INT            NOT NULL,
    fila_origen        INT            NOT NULL,
    hash_registro      BINARY(32)     NOT NULL,
    credito_id         BIGINT         NOT NULL,
    codigo_origen      VARCHAR(30)    NOT NULL,
    celular            VARCHAR(10)    NOT NULL,
    codigo_corto       INT            NOT NULL,
    fecha_programacion DATETIME2(0)   NOT NULL,
    fecha_envio        DATETIME2(0)   NOT NULL,
    estado_envio       VARCHAR(40)    NOT NULL,
    operador           VARCHAR(30)    NOT NULL,
    campania           VARCHAR(60)    NOT NULL,
    tipo_mensaje       VARCHAR(30)    NOT NULL,
    perfil_historico   VARCHAR(80)    NOT NULL,
    numero_mensajes    SMALLINT       NOT NULL,
    total_clics        INT            NOT NULL,
    hash_mensaje       BINARY(32)     NOT NULL,
    mensaje            NVARCHAR(1000) NOT NULL
);
GO

IF OBJECT_ID('dw.fact_sms') IS NULL
CREATE TABLE dw.fact_sms (
    sms_id             BIGINT IDENTITY CONSTRAINT pk_fact_sms PRIMARY KEY,
    hash_registro      BINARY(32)    NOT NULL,   -- llave natural: crédito+celular+fechas+tipo+código corto
    archivo_id         INT           NOT NULL CONSTRAINT fk_sms_archivo   REFERENCES etl.control_archivo(archivo_id),
    credito_id         BIGINT        NOT NULL CONSTRAINT fk_sms_credito   REFERENCES dw.dim_credito(credito_id),
    fecha_id           INT           NOT NULL CONSTRAINT fk_sms_fecha     REFERENCES dw.dim_fecha(fecha_id),
    plantilla_id       INT           NOT NULL CONSTRAINT fk_sms_plantilla REFERENCES dw.dim_plantilla_sms(plantilla_id),
    codigo_origen      VARCHAR(30)   NOT NULL,
    celular            VARCHAR(10)   NOT NULL CONSTRAINT ck_sms_celular CHECK (celular LIKE '3[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]'),
    codigo_corto       INT           NOT NULL,
    fecha_programacion DATETIME2(0)  NOT NULL,
    fecha_envio        DATETIME2(0)  NOT NULL,
    estado_envio       VARCHAR(40)   NOT NULL,
    es_entregado       AS (CASE WHEN estado_envio = 'Entregado a la red' THEN 1 ELSE 0 END) PERSISTED,
    operador           VARCHAR(30)   NOT NULL,
    campania           VARCHAR(60)   NOT NULL,
    tipo_mensaje       VARCHAR(30)   NOT NULL,
    perfil_historico   VARCHAR(80)   NOT NULL,
    numero_mensajes    SMALLINT      NOT NULL CONSTRAINT ck_sms_num_msj CHECK (numero_mensajes >= 1),
    total_clics        INT           NOT NULL CONSTRAINT ck_sms_clics   CHECK (total_clics >= 0),
    fecha_carga        DATETIME2(0)  NOT NULL CONSTRAINT df_sms_carga DEFAULT SYSDATETIME(),
    CONSTRAINT uq_fact_sms_hash UNIQUE (hash_registro),           -- garantiza idempotencia
    CONSTRAINT ck_sms_fechas CHECK (fecha_envio >= fecha_programacion)
);
IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_sms_credito_fecha')
    CREATE INDEX ix_sms_credito_fecha ON dw.fact_sms (credito_id, fecha_id);
GO
