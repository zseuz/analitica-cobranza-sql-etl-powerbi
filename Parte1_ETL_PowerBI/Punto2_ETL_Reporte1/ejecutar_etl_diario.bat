@echo off
REM Ejecución diaria del ETL del Reporte1 (la invoca el Programador de tareas de Windows).
REM Procesa todos los archivos que haya en Punto2_ETL_Reporte1\entrada\reporte1, los mueve a
REM Punto2_ETL_Reporte1\procesados\ y deja la bitácora en Parte1_ETL_PowerBI\logs\etl_reporte1_AAAAMMDD.log.
REM Código de salida 1 si algún archivo falla.
REM Se ejecuta desde la carpeta raíz del proyecto (dos niveles arriba de este archivo).
cd /d "%~dp0..\.."
python -m Parte1_ETL_PowerBI.Punto2_ETL_Reporte1.etl_reporte1 %*
exit /b %ERRORLEVEL%
