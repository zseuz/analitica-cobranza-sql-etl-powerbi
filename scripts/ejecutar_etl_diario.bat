@echo off
REM Ejecución diaria del ETL del Reporte1 (la invoca el Programador de tareas de Windows).
REM Procesa todos los archivos que haya en entrada\reporte1, los mueve a
REM procesados\ y deja la bitácora en logs\etl_reporte1_AAAAMMDD.log.
REM Código de salida 1 si algún archivo falla.
REM Se ejecuta desde la carpeta raíz del proyecto (un nivel arriba de este archivo).
cd /d "%~dp0.."
python -m etl.etl_reporte1 %*
exit /b %ERRORLEVEL%
