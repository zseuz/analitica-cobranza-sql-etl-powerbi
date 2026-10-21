# Descubrimientos más relevantes

Desarrollador de Inteligencia de Negocio · SQL Server, Python y Power BI

## 1. Resumen ejecutivo

- Las **4 bases quedaron cargadas** en SQL Server (`AnaliticaCobranza`) con un modelo relacional de 13 llaves foráneas y 11 restricciones de integridad. Ninguna fila se descartó sin dejar rastro: lo descartado quedó en `etl.rechazo` con su motivo.
- Las fuentes tienen **problemas de calidad deliberados y relevantes**: el 25 % de los IDs de crédito vienen con texto basura, el 22 % de las gestiones están duplicadas y las categorías aparecen escritas de cientos de formas.
- **El cuello de botella de la cobranza no es la negociación, es el cumplimiento.** El 54,5 % de los contactos directos termina en promesa, pero solo el 34,5 % de las promesas vencidas se cumple. Medido en dinero, solo se pagó el **11 %** del valor prometido.
- **La hora de la llamada importa más que el volumen.** La contactabilidad es del 32 % a las 8 a. m. y del 2 % a las 6 p. m., pero el mayor volumen de llamadas se concentra entre las 10 a. m. y el mediodía, cuando contesta menos del 9 %.
- **La cobertura es baja y no está priorizada.** Solo el 10,3 % de la cartera recibe gestión, y ese porcentaje es prácticamente el mismo para un crédito al día que para uno con más de 360 días de mora.

## 2. Calidad de los datos

| Hallazgo | Magnitud | Tratamiento aplicado |
|---|---|---|
| ID de crédito con prefijos y sufijos basura (`OP-1685153-A`, `C_1135300_OK`, `CTO1323881X`) | ~25 % de las filas de Recaudo, Comité y Gestiones; 35 patrones distintos | Se extrae el único bloque numérico como llave (`credito_id`) y se conserva el código original para trazabilidad |
| Categorías escritas de muchas formas (`'Sin-Contacto.'`, `' #Sin Contacto '`, `'banco bogota_'`, `'Normal*'`) | 1.032 variantes de tipificación, 893 de origen de pago y 492 de estado de cartera | Normalización (minúsculas, sin tildes ni símbolos) y homologación a catálogos cerrados: 25, 16 y 10 valores |
| Gestiones duplicadas | 19.673 de 89.485 (22 %), detectadas después de homologar la tipificación | Se eliminan; así no inflan el volumen ni la intensidad de gestión |
| Pagos duplicados exactos | 468 de 74.966 | Se eliminan; la fuente no trae un ID de transacción |
| Mismo crédito y mes repetido en Comité al normalizar el ID | 240 | Se conserva la foto de mayor mora (criterio conservador) |
| Días de mora = 999 | 1.711 registros, el 91 % en estado Judicial | Se marca como valor centinela y se deja en un tramo propio ("Sin dato (999)") |
| Estado de cartera `'0'`, tasa nominal 0, cuotas pagadas negativas | 148 / 16 / 9 | "Sin información" / NULL / NULL |
| Orígenes de pago que no son recaudo (Ajuste, Otros, Otros sin información) | 49 pagos por $158 M | Se marcan como no efectivos y se excluyen del recaudo, como indica la hoja Recaudo |
| Reversiones (valores negativos) | 3.936 por –$14.149 M (6,5 % del recaudo bruto) | Se conservan para que el recaudo quede neto |

## 3. Hallazgos de negocio

### 3.1 Embudo de gestión

| Etapa | Valor | Lectura |
|---|---:|---|
| Intentos de contacto telefónico | 33.156 | Base de la contactabilidad |
| Tasa de contactabilidad | 9,4 % | 9 de cada 100 llamadas logran hablar con alguien |
| Tasa de contacto directo (titular) | 7,8 % | Techo real de la negociación |
| Tasa PTP (promesas / contactos directos) | 54,5 % | La negociación funciona bien |
| Kept rate (promesas cumplidas / vencidas) | 34,5 % | **Aquí se pierde la mayor parte del valor** |
| Monto cumplido / monto prometido | 11,3 % | Las promesas grandes casi no se cumplen |

- Los **acuerdos de pago** formales se cumplen más (47 %) que las "promesas" (34 %) y las "posibilidades de pago" (34 %). Formalizar el compromiso mejora el cumplimiento.
- 130 promesas no traen valor prometido. Así no se puede medir qué tan completo fue el pago.

### 3.2 Franja horaria

| Hora | Intentos | Contactabilidad |
|---:|---:|---:|
| 8 a. m. | 1.737 | 31,8 % |
| 9 a. m. | 2.107 | 19,2 % |
| 10 a. m. – 1 p. m. | 17.276 | 5,6 % – 8,7 % |
| 2 p. m. | 2.855 | 14,0 % |
| 5 – 6 p. m. | 3.460 | 1,6 % – 3,2 % |

Mover parte del volumen de mitad de mañana hacia las 8–9 a. m. y las 2 p. m. podría multiplicar los contactos sin agregar asesores.

### 3.3 Cobertura y priorización

| Tramo de mora | Créditos | Con gestión | Cobertura |
|---|---:|---:|---:|
| Al día | 17.746 | 1.742 | 9,8 % |
| 1–30 | 6.515 | 704 | 10,8 % |
| 31–90 | 1.042 | 106 | 10,2 % |
| 91–360 | 1.279 | 130 | 10,2 % |
| +360 | 1.016 | 106 | 10,4 % |

La cobertura es casi idéntica en todos los tramos. La asignación parece aleatoria y no basada en riesgo.

### 3.4 Recaudo

- El recaudo efectivo neto es de **$202.882 M**. El **77 %** ($155.362 M) corresponde a créditos que tuvieron una gestión o un SMS en los 30 días previos al pago.
- Los principales canales de pago son Caja (29 %), PSE (28 %) y Banco de Bogotá (24 %).
- El índice de cartera vencida (saldo con más de 30 días de mora / saldo total) es del **12,4 %**.

### 3.5 Canal SMS (Reporte1)

- 7.438 SMS a 2.740 créditos (2,7 por crédito), con 100 % de entrega a la red y **0 clics**. El SMS funciona como recordatorio, no como canal de autogestión: el mensaje no trae enlace de pago.
- El 67 % de los mensajes son de "compromiso incumplido" y el 33 % son recordatorios de pago.

## 4. Limitaciones de las fuentes

- **El Reporte1 es de julio de 2022 y las demás bases son de junio–agosto de 2025.** Se cruzan por crédito (los 2.740 créditos del Reporte1 existen en Comité), pero no por fecha. Por eso no se puede medir si un SMS llevó a un pago.
- **Comité no es un panel completo.** Cada mes trae ~10.000 créditos casi distintos (solo 483 aparecen en dos meses). Por eso cada gestión toma la foto de cartera del mismo mes o, si no existe, la más cercana.
- **No hay asesor ni agente** en ninguna base. El tablero filtra por fecha, canal, tramo de mora, estado y producto.
- **Las bases 3 y 4 no traen celular ni valor de cuota**, que la Parte 2 exige. El celular sale del Reporte1 (53 de 437 promesas quedan sin él) y la cuota se estima como anualidad con el saldo, la tasa nominal mensual y las cuotas pendientes.
- **Los datos terminan el 29-ago-2025.** Las vistas de la Parte 2 usan una fecha de referencia configurable (25-jul-2025 en la demostración); en producción se deja vacía para usar la fecha del día.

## 5. Recomendaciones

1. **Reasignar la franja de llamadas** hacia las 8–9 a. m. y las 2 p. m., y medir el efecto en la contactabilidad con el mismo tablero.
2. **Atacar el cumplimiento, no la negociación.** Convertir las promesas en acuerdos formales (47 % de cumplimiento frente a 34 %), confirmarlas por SMS el día antes y hacer seguimiento diario con la Vista 1 de la Parte 2.
3. **Priorizar la cobertura por riesgo** (tramos 31–180 días y la Vista 2 de mora +60) en lugar de repartirla de forma uniforme.
4. **Agregar un enlace de pago (PSE) al SMS.** Hoy tiene 0 clics, y PSE ya es el segundo canal de recaudo.
5. **Corregir la calidad en el origen**: ID de crédito sin sufijos, catálogos cerrados en el aplicativo de gestión, ID de transacción en los pagos e identificación del asesor en cada gestión.

## 6. Punto opcional: resumen ejecutivo con IA

- **Alternativa elegida:** narrativa automática para el dashboard (página "Resumen IA").
- **API:** Google Gemini API (plan gratuito de Google AI Studio, SDK `google-genai`), modelo `gemini-flash-latest`. El script admite Anthropic Claude como proveedor alternativo.
- **Lógica:** `etl/resumen_ia.py` calcula los KPIs agregados del data mart (embudo, cobertura por tramo, contactabilidad por hora, cumplimiento por tipo de promesa, recaudo, SMS). Los envía al modelo y guarda la respuesta en la tabla `mart.resumen_ia`, que el dashboard muestra. **No se envía ningún dato de clientes.**
- **Prompt:** el modelo actúa como analista senior de cobranza que escribe para la gerencia. Usa solo las cifras recibidas, en formato colombiano, sin inventar metas ni causas. Prioriza dónde se pierde valor en el embudo contacto → promesa → pago.
- **Salida:** JSON con esquema fijo (titular, resumen, alertas con severidad, recomendaciones), validado antes de guardarse. Incluye reintentos y un modelo de respaldo ante saturación del servicio.
- **Resultado:** al verificar las cifras citadas contra los KPIs, todas fueron correctas salvo una generalización sobre la cobertura por tramo. Por eso el tablero advierte revisar las cifras: la IA redacta y el data mart sigue siendo la fuente de verdad.
- **Valor agregado:** la gerencia recibe en lenguaje natural lo que el tablero muestra en números. Se regenera con cada carga y no requiere que un analista lo redacte.

## 7. Contenido del proyecto

| Carpeta | Contenido |
|---|---|
| `1_Parte1_ETL` | `sql/01_modelo.sql` (modelo relacional), `sql/02_mart.sql` (tablas finales), `etl/*.py` (carga de las 4 bases, ETL diario del Reporte1, limpieza, data mart y resumen con IA), `run_reporte1.bat` y `programar_tarea.ps1` (ejecución diaria automática) |
| `2_Parte2_Vistas` | `03_vistas_promesas.sql` (vistas creadas en el esquema `seg` de la base `AnaliticaCobranza`) |
| `3_Dashboard` | `Dashboard_Cobranza.pbix` |
| `4_Parte3_Analisis` | `Parte3_Analisis_Script_SQL.docx` (incluye la propuesta optimizada como anexo) |
| `5_Descubrimientos` | `Descubrimientos.docx` (este documento) |
