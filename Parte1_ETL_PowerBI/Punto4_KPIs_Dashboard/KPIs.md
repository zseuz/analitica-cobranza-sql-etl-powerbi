# Parte 1, punto 4 – KPIs y dashboard de Power BI

**Archivo:** `powerbi/Dashboard_Cobranza.pbip` (proyecto de Power BI). Se regenera con `python powerbi/generar_pbip.py`.
Para usarlo: abrirlo en Power BI Desktop → **Actualizar** → *Archivo › Guardar como* → `Dashboard_Cobranza.pbix`.

## Modelo
El modelo es un esquema estrella sobre el data mart del punto 3 (esquema `mart` de SQL Server, en modo importación).

| Relación | Cardinalidad |
|---|---|
| `dim_fecha[fecha_id]` → `fact_gestiones`, `fact_recaudo`, `fact_promesas[fecha_promesa_id]` | 1:N |
| `dim_fecha[fecha_id]` → `fact_promesas[fecha_gestion_id]` (inactiva, para usar con `USERELATIONSHIP`) | 1:N |
| `dim_fecha[fecha]` → `fact_cartera_mes[fecha_mes]` | 1:N |
| `dim_credito[credito_id]` → las 4 tablas de hechos | 1:N |

**Segmentadores en todas las páginas:** fecha (rango), canal de gestión, tramo de mora, estado de cartera y producto.
Las fuentes no traen asesor ni agente, así que ese segmentador no se puede construir.

**Páginas**
1. **Resumen ejecutivo**: 7 tarjetas de KPI, gestiones por canal, contactabilidad y PTP por tramo, estado de las promesas, embudo diario y recaudo diario.
2. **Promesas de pago**: PTP, kept rate, prometido vs. pagado, promesas por día y estado, y el detalle.
3. **Canal SMS (Reporte1)**: volumen, entrega, CTR, SMS por crédito, por operador, por tipo, por hora y por tramo.
4. **Definición de KPIs**: la justificación de cada métrica dentro del propio tablero.

## Utilidad operativa y gerencial de cada métrica

- **Tasa de contactabilidad = contactos / intentos telefónicos**: Mide la calidad de la base de teléfonos y la efectividad del marcador. Si baja, hay que depurar números o cambiar franjas horarias.
- **Tasa de contacto directo = contactos con el titular / intentos**: Solo el titular puede comprometerse a pagar. Es el verdadero techo de la gestión telefónica.
- **Tasa PTP = promesas / contactos directos**: Mide la capacidad de negociación de los asesores. Una PTP baja con contacto alto apunta a guion, capacitación u ofertas de normalización.
- **Kept rate = promesas cumplidas / promesas vencidas**: Mide la calidad de las promesas. Una PTP alta con kept rate bajo indica promesas 'de salida' que no generan caja.
- **Cobertura de cartera = créditos gestionados / créditos en cartera**: Indica qué parte del portafolio se está tocando. Sirve para dimensionar la capacidad operativa y priorizar tramos sin gestión.
- **Intensidad de gestión = gestiones / créditos gestionados**: Esfuerzo por crédito. Comparada con el recaudo, muestra si insistir más genera pago o solo costo (y riesgo de acoso al cliente).
- **Recaudo efectivo y % con gestión previa**: Resultado final en caja, sin ajustes ni 'otros', neto de reversiones. El % con gestión previa aproxima el aporte de la cobranza al recaudo.
- **Índice de cartera vencida (saldo mora +30 / saldo)**: Salud del portafolio y contexto para leer los demás KPIs por tramo de mora.
- **SMS: volumen, entrega, CTR y SMS por crédito**: Eficiencia del canal digital de menor costo. Con 0 clics en el Reporte1, el SMS funciona como recordatorio y no como canal de autogestión.
- **Nota sobre asesor**: Las fuentes no traen asesor ni agente. El tablero segmenta por fecha, canal, tramo de mora, estado y producto.

## Medidas DAX (tabla `_Medidas`)


### 1. Gestión

| Medida | DAX | Formato | Descripción |
|---|---|---|---|
| Gestiones | `COUNTROWS(fact_gestiones)` | `#,0` | Total de gestiones (SMS + gestiones de cobranza). |
| Créditos gestionados | `DISTINCTCOUNT(fact_gestiones[credito_id])` | `#,0` | Créditos distintos con al menos una gestión o SMS. |
| Intensidad de gestión | `DIVIDE([Gestiones], [Créditos gestionados])` | `#,0.0` | Gestiones promedio por crédito gestionado. |
| Créditos en cartera | `DISTINCTCOUNT(dim_credito[credito_id])` | `#,0` | Créditos del portafolio (Comité). |
| Cobertura de cartera | `DIVIDE([Créditos gestionados], [Créditos en cartera])` | `0.0%` | % de la cartera que recibió al menos una gestión. |
| Cobertura cartera en mora | `DIVIDE(CALCULATE([Créditos gestionados], dim_credito[dias_mora] > 0), CALCULATE([Créditos en cartera], dim_credito[dias_mora] > 0))` | `0.0%` | % de los créditos en mora que recibieron gestión. |

### 2. Contacto

| Medida | DAX | Formato | Descripción |
|---|---|---|---|
| Intentos de contacto | `CALCULATE(COUNTROWS(fact_gestiones), fact_gestiones[es_intento_contacto] = TRUE())` | `#,0` | Gestiones telefónicas (denominador de contactabilidad). |
| Contactos | `CALCULATE(COUNTROWS(fact_gestiones), fact_gestiones[es_contacto] = TRUE())` | `#,0` | Intentos en los que se habló con alguien (titular o tercero). |
| Contactos directos | `CALCULATE(COUNTROWS(fact_gestiones), fact_gestiones[es_contacto_directo] = TRUE())` | `#,0` | Intentos en los que se habló con el titular. |
| Tasa de contactabilidad | `DIVIDE([Contactos], [Intentos de contacto])` | `0.0%` | Contactos / intentos de contacto. |
| Tasa de contacto directo | `DIVIDE([Contactos directos], [Intentos de contacto])` | `0.0%` | Contactos con el titular / intentos de contacto. |

### 3. Promesas

| Medida | DAX | Formato | Descripción |
|---|---|---|---|
| Promesas generadas | `CALCULATE(COUNTROWS(fact_gestiones), fact_gestiones[es_promesa] = TRUE())` | `#,0` | Gestiones que terminaron en promesa de pago (por fecha de gestión). |
| Tasa PTP | `DIVIDE([Promesas generadas], [Contactos directos])` | `0.0%` | Promise to Pay: promesas / contactos directos. |
| Promesas | `COUNTROWS(fact_promesas)` | `#,0` | Promesas (por fecha prometida). |
| Promesas vencidas | `CALCULATE([Promesas], fact_promesas[es_vencida] = TRUE())` | `#,0` | Promesas cuya fecha límite ya pasó a la fecha de corte. |
| Promesas cumplidas | `CALCULATE([Promesas], fact_promesas[es_vencida] = TRUE(), fact_promesas[es_cumplida] = TRUE())` | `#,0` | Promesas vencidas con pago efectivo >= 90 % de lo prometido. |
| Promesas incumplidas | `[Promesas vencidas] - [Promesas cumplidas]` | `#,0` | Promesas vencidas sin pago suficiente. |
| Kept rate | `DIVIDE([Promesas cumplidas], [Promesas vencidas])` | `0.0%` | % de promesas vencidas que se cumplieron. |
| Valor prometido | `SUM(fact_promesas[valor_promesa])` | `$#,0` | Monto total prometido. |
| Valor pagado sobre promesas | `SUM(fact_promesas[valor_pagado])` | `$#,0` | Recaudo efectivo recibido dentro de la ventana de cada promesa. |

### 4. Recaudo

| Medida | DAX | Formato | Descripción |
|---|---|---|---|
| Recaudo efectivo | `CALCULATE(SUM(fact_recaudo[valor_total_pago]), fact_recaudo[es_recaudo_efectivo] = TRUE())` | `$#,0` | Pagos efectivos netos de reversiones (excluye Ajuste / Otros / Otros sin información). |
| Recaudo con gestión previa | `CALCULATE([Recaudo efectivo], fact_recaudo[tuvo_gestion_previa_30d] = TRUE())` | `$#,0` | Recaudo de créditos gestionados en los 30 días previos al pago. |
| % recaudo con gestión previa | `DIVIDE([Recaudo con gestión previa], [Recaudo efectivo])` | `0.0%` | Participación del recaudo asociada a la gestión. |
| Reversiones | `CALCULATE(SUM(fact_recaudo[valor_total_pago]), fact_recaudo[es_reversion] = TRUE())` | `$#,0` | Valor reversado (negativo). |

### 5. Cartera

| Medida | DAX | Formato | Descripción |
|---|---|---|---|
| Saldo de cartera | `SUM(fact_cartera_mes[saldo_capital])` | `$#,0` | Saldo de capital (Comité). |
| Saldo en mora +30 | `CALCULATE([Saldo de cartera], fact_cartera_mes[dias_mora] > 30)` | `$#,0` | Saldo de créditos con más de 30 días de mora. |
| Índice de cartera vencida | `DIVIDE([Saldo en mora +30], [Saldo de cartera])` | `0.0%` | ICV: saldo en mora +30 / saldo total. |

### 6. SMS

| Medida | DAX | Formato | Descripción |
|---|---|---|---|
| SMS enviados | `CALCULATE(COUNTROWS(fact_gestiones), fact_gestiones[canal] = "SMS")` | `#,0` | Mensajes del Reporte1. |
| Créditos con SMS | `CALCULATE(DISTINCTCOUNT(fact_gestiones[credito_id]), fact_gestiones[canal] = "SMS")` | `#,0` | Créditos distintos contactados por SMS. |
| Tasa de entrega SMS | `DIVIDE(CALCULATE(COUNTROWS(fact_gestiones), fact_gestiones[canal] = "SMS", fact_gestiones[es_entregado] = TRUE()), [SMS enviados])` | `0.0%` | SMS entregados a la red / enviados. |
| CTR SMS | `DIVIDE(CALCULATE(SUM(fact_gestiones[total_clics]), fact_gestiones[canal] = "SMS"), [SMS enviados])` | `0.00%` | Clics / SMS enviados. |
| SMS por crédito | `DIVIDE([SMS enviados], [Créditos con SMS])` | `#,0.0` | Intensidad del canal SMS. |

## Valores de control (fecha de corte 29-ago-2025, sin filtros)

Verificados con SQL directamente sobre el data mart, sirven para comprobar que el tablero muestra lo mismo:

| KPI | Valor |
|---|---:|
| Gestiones | 77.250 (69.812 de cobranza + 7.438 SMS) |
| Tasa de contactabilidad | 9,4 % |
| Tasa de contacto directo | 7,8 % |
| Tasa PTP | 54,5 % |
| Kept rate | 34,5 % |
| Cobertura de cartera | 10,3 % (3.019 de 29.277 créditos con gestión de cobranza) |
| Intensidad de gestión | 25,6 gestiones por crédito gestionado (77.250 / 3.019, SMS incluidos); 23,1 filtrando solo gestiones de cobranza |
