# Registra la tarea diaria del ETL del Reporte1 en el Programador de tareas de Windows.
# Uso:  powershell -ExecutionPolicy Bypass -File programar_tarea_diaria.ps1 [-Hora "06:00"]
param([string]$Hora = "06:00")

$bat = Join-Path $PSScriptRoot "ejecutar_etl_diario.bat"
$accion = New-ScheduledTaskAction -Execute $bat -WorkingDirectory $PSScriptRoot
$disparador = New-ScheduledTaskTrigger -Daily -At $Hora
# StartWhenAvailable: si el equipo estaba apagado a la hora programada, la tarea corre al encenderlo.
# RestartCount/RestartInterval: si la tarea no logra iniciarse, Windows lo reintenta 3 veces cada 30 min.
# (Un archivo que falla al cargarse no necesita reintento aquí: queda en la carpeta de entrada y se
#  vuelve a procesar en la siguiente ejecución.)
$opciones = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 30)

Register-ScheduledTask -TaskName "AnaliticaCobranza - ETL Reporte1" -Action $accion -Trigger $disparador `
    -Settings $opciones -Description "Carga diaria del Reporte1 (SMS de cobranza) a SQL Server" -Force
Write-Host "Tarea programada todos los días a las $Hora"
