/* =====================================================================
   Parte 1, punto 3: estructura del data mart (esquema mart)
   ---------------------------------------------------------------------
   Este script SOLO define las tablas finales que consume el dashboard.
   Toda la lógica que las llena (unificación de canales, cumplimiento de
   promesas, cobertura, etc.) está en Python:  etl/data_mart.py
   Las tablas son derivadas: se pueden borrar y reconstruir en cualquier momento.
   ===================================================================== */
USE AnaliticaCobranza;
GO

/* ---------------------------------------------------------------------
   Tablas del mart (derivadas: se pueden borrar y reconstruir siempre)
   --------------------------------------------------------------------- */
DROP TABLE IF EXISTS mart.fact_gestiones, mart.fact_promesas, mart.fact_cartera_mes,
                     mart.fact_recaudo, mart.dim_credito, mart.dim_fecha;
GO

CREATE TABLE mart.dim_credito (
    credito_id           BIGINT        NOT NULL CONSTRAINT pk_mart_dim_credito PRIMARY KEY,
    periodo_ultima_foto  INT           NULL,
    tipo_producto        SMALLINT      NULL,
    estado_cartera       VARCHAR(30)   NULL,
    dias_mora            SMALLINT      NULL,
    tramo_mora           VARCHAR(20)   NULL,
    saldo_capital        DECIMAL(18,2) NULL,
    valor_cuota_estimada DECIMAL(18,2) NULL,
    celular              VARCHAR(10)   NULL,     -- último celular conocido (Reporte1)
    tiene_sms            BIT           NOT NULL,
    tiene_gestion        BIT           NOT NULL,
    tiene_recaudo        BIT           NOT NULL
);

CREATE TABLE mart.fact_gestiones (
    gestion_key          VARCHAR(20)   NOT NULL CONSTRAINT pk_mart_fact_gestiones PRIMARY KEY, -- 'S-<id>' SMS / 'G-<id>' gestión
    fuente               VARCHAR(20)   NOT NULL,   -- 'Reporte1 (SMS)' / 'Gestiones'
    canal                VARCHAR(30)   NOT NULL,   -- SMS / Telefónica / Mensajería / Notificación / Administrativa
    credito_id           BIGINT        NOT NULL,
    fecha_id             INT           NOT NULL,
    fecha                DATE          NOT NULL,
    fecha_hora           DATETIME2(0)  NOT NULL,
    hora                 TINYINT       NOT NULL,
    resultado            VARCHAR(80)   NOT NULL,   -- tipificación (gestiones) o tipo de mensaje (SMS)
    categoria            VARCHAR(30)   NOT NULL,
    es_intento_contacto  BIT           NOT NULL,   -- denominador de contactabilidad
    es_contacto          BIT           NOT NULL,
    es_contacto_directo  BIT           NOT NULL,
    es_promesa           BIT           NOT NULL,
    valor_promesa        DECIMAL(18,2) NOT NULL,
    fecha_promesa        DATE          NULL,
    -- solo SMS
    operador             VARCHAR(30)   NULL,
    es_entregado         BIT           NULL,
    numero_mensajes      SMALLINT      NULL,
    total_clics          INT           NULL,
    -- atributos de cartera (foto del mismo mes; si no existe, la más cercana)
    periodo_foto         INT           NULL,
    foto_mismo_mes       BIT           NOT NULL,
    tipo_producto        SMALLINT      NULL,
    estado_cartera       VARCHAR(30)   NULL,
    dias_mora            SMALLINT      NULL,
    tramo_mora           VARCHAR(20)   NULL,
    saldo_capital        DECIMAL(18,2) NULL
);
CREATE INDEX ix_mart_gest_credito ON mart.fact_gestiones (credito_id, fecha_id);

CREATE TABLE mart.fact_promesas (
    gestion_id           BIGINT        NOT NULL CONSTRAINT pk_mart_fact_promesas PRIMARY KEY,
    credito_id           BIGINT        NOT NULL,
    fecha_gestion        DATE          NOT NULL,
    fecha_gestion_id     INT           NOT NULL,
    fecha_promesa        DATE          NOT NULL,
    fecha_promesa_id     INT           NOT NULL,
    fecha_limite         DATE          NOT NULL,   -- fecha_promesa + días de gracia
    tipificacion         VARCHAR(80)   NOT NULL,
    valor_promesa        DECIMAL(18,2) NOT NULL,
    valor_pagado         DECIMAL(18,2) NOT NULL,   -- recaudo efectivo entre gestión y fecha límite
    fecha_primer_pago    DATE          NULL,
    porc_cumplimiento    DECIMAL(9,4)  NULL,
    estado_promesa       VARCHAR(30)   NOT NULL,   -- Cumplida / Incumplida / Incumplida con pago parcial / Vigente
    es_vencida           BIT           NOT NULL,   -- ya pasó la fecha límite (base del kept rate)
    es_cumplida          BIT           NOT NULL,
    dias_mora            SMALLINT      NULL,
    tramo_mora           VARCHAR(20)   NULL,
    estado_cartera       VARCHAR(30)   NULL,
    fecha_corte          DATE          NOT NULL
);

CREATE TABLE mart.fact_cartera_mes (
    credito_id            BIGINT        NOT NULL,
    periodo               INT           NOT NULL,
    fecha_mes             DATE          NOT NULL,   -- 1.er día del mes (relación con calendario)
    tipo_producto         SMALLINT      NOT NULL,
    estado_cartera        VARCHAR(30)   NOT NULL,
    dias_mora             SMALLINT      NOT NULL,
    tramo_mora            VARCHAR(20)   NOT NULL,
    es_en_mora            BIT           NOT NULL,
    saldo_capital         DECIMAL(18,2) NOT NULL,
    valor_cuota_estimada  DECIMAL(18,2) NULL,
    gestiones             INT           NOT NULL,
    intentos_contacto     INT           NOT NULL,
    contactos             INT           NOT NULL,
    contactos_directos    INT           NOT NULL,
    promesas              INT           NOT NULL,
    valor_prometido       DECIMAL(18,2) NOT NULL,
    sms                   INT           NOT NULL,
    es_gestionado         BIT           NOT NULL,   -- tuvo al menos una gestión en el mes (cobertura)
    es_contactado         BIT           NOT NULL,
    recaudo_efectivo      DECIMAL(18,2) NOT NULL,   -- pagos efectivos netos de reversiones en el mes
    tiene_pago            BIT           NOT NULL,
    CONSTRAINT pk_mart_fact_cartera_mes PRIMARY KEY (credito_id, periodo)
);

CREATE TABLE mart.fact_recaudo (
    recaudo_id               BIGINT        NOT NULL CONSTRAINT pk_mart_fact_recaudo PRIMARY KEY,
    credito_id               BIGINT        NOT NULL,
    fecha_id                 INT           NOT NULL,
    fecha                    DATE          NOT NULL,
    origen_pago              VARCHAR(40)   NOT NULL,
    es_recaudo_efectivo      BIT           NOT NULL,
    es_reversion             BIT           NOT NULL,
    valor_total_pago         DECIMAL(18,2) NOT NULL,
    valor_pago_capital       DECIMAL(18,2) NOT NULL,
    valor_pago_intereses_mora DECIMAL(18,2) NOT NULL,
    tuvo_gestion_previa_30d  BIT           NOT NULL    -- hubo gestión/SMS al crédito en los 30 días previos
);
GO

CREATE TABLE mart.dim_fecha (
    fecha_id      INT          NOT NULL CONSTRAINT pk_mart_dim_fecha PRIMARY KEY,
    fecha         DATE         NOT NULL,
    anio          SMALLINT     NOT NULL,
    mes           TINYINT      NOT NULL,
    nombre_mes    VARCHAR(12)  NOT NULL,
    periodo       INT          NOT NULL,
    dia           TINYINT      NOT NULL,
    dia_semana    TINYINT      NOT NULL,
    nombre_dia    VARCHAR(10)  NOT NULL,
    es_fin_semana BIT          NOT NULL,
    fecha_mes     DATE         NOT NULL     -- 1.er día del mes (relación con fact_cartera_mes)
);
GO

/* ---------------------------------------------------------------------
   Punto opcional (IA): resumen ejecutivo generado con IA (Gemini por defecto
   o Claude) por etl/resumen_ia.py. No se borra al recrear el mart:
   guarda el último resumen generado, que el dashboard muestra en la página "Resumen IA".
   --------------------------------------------------------------------- */
IF OBJECT_ID('mart.resumen_ia') IS NULL
CREATE TABLE mart.resumen_ia (
    resumen_id    INT IDENTITY  NOT NULL CONSTRAINT pk_mart_resumen_ia PRIMARY KEY,
    generado_en   DATETIME2(0)  NOT NULL,
    modelo        VARCHAR(50)   NOT NULL,
    fecha_corte   DATE          NOT NULL,
    seccion       VARCHAR(20)   NOT NULL CONSTRAINT ck_resumen_ia_seccion
                  CHECK (seccion IN ('Titular', 'Resumen', 'Alerta', 'Recomendación')),
    orden_seccion TINYINT       NOT NULL,
    orden         TINYINT       NOT NULL,
    orden_global  SMALLINT      NOT NULL,          -- orden_seccion * 100 + orden (orden de lectura)
    etiqueta      VARCHAR(30)   NOT NULL,          -- 'Titular', 'Alerta 2', 'Recomendación 1'...
    titulo        NVARCHAR(200) NULL,
    texto         NVARCHAR(2000) NOT NULL,
    severidad     VARCHAR(10)   NULL
);
GO
