<#
.SYNOPSIS
    Registra el Bridge como tarea programada para que arranque solo.

.DESCRIPTION
    Por que una tarea al INICIAR SESION y no un servicio de Windows:

    Un servicio corre en la "sesion 0", que no tiene escritorio. MT5 es una
    aplicacion grafica y ahi se comporta de forma impredecible: MetaQuotes no
    soporta ese modo. Como el Bridge lanza un terminal MT5 por cuenta, nos
    interesa una sesion interactiva de verdad.

    La consecuencia es que el servidor necesita inicio de sesion automatico
    para que, tras un reinicio, exista esa sesion sin que nadie entre por RDP.
    Eso se configura aparte (ver README).

.EXAMPLE
    .\scripts\install-bridge-task.ps1
    .\scripts\install-bridge-task.ps1 -Desinstalar
#>

param(
    [string] $Nombre = "Bridge Manager",
    [switch] $Desinstalar
)

$ErrorActionPreference = "Stop"

$raiz   = Split-Path -Parent $PSScriptRoot
$script = Join-Path $PSScriptRoot "run-bridge.ps1"
$quien  = "$env:USERDOMAIN\$env:USERNAME"

if (Get-ScheduledTask -TaskName $Nombre -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $Nombre -Confirm:$false
    Write-Host "Tarea anterior '$Nombre' eliminada."
}
if ($Desinstalar) {
    Write-Host "Listo. El Bridge ya no arranca solo."
    return
}

if (-not (Test-Path $script)) { throw "No encuentro $script" }

$accion = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`"" `
    -WorkingDirectory $raiz

$disparador = New-ScheduledTaskTrigger -AtLogOn -User $quien

# RunLevel Highest porque los terminales MT5 se lanzan como procesos hijos.
$principal = New-ScheduledTaskPrincipal `
    -UserId $quien -LogonType Interactive -RunLevel Highest

# ExecutionTimeLimit 0 = sin limite: es un proceso que no termina nunca.
# IgnoreNew evita levantar un segundo Bridge sobre el mismo pool de slots.
$ajustes = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName $Nombre `
    -Action $accion -Trigger $disparador -Principal $principal -Settings $ajustes `
    -Description "Arranca el Bridge Manager al iniciar sesion. Log en $raiz\logs." | Out-Null

Write-Host ""
Write-Host "Tarea '$Nombre' registrada para $quien."
Write-Host ""
Write-Host "  Arrancar ahora:  Start-ScheduledTask -TaskName '$Nombre'"
Write-Host "  Ver estado:      Get-ScheduledTask -TaskName '$Nombre' | Get-ScheduledTaskInfo"
Write-Host "  Parar:           Stop-ScheduledTask -TaskName '$Nombre'"
Write-Host "  Log de hoy:      Get-Content $raiz\logs\bridge-$(Get-Date -Format 'yyyyMMdd').log -Tail 40 -Wait"
Write-Host ""
Write-Host "FALTA el inicio de sesion automatico para que sobreviva a un"
Write-Host "reinicio sin que nadie entre por RDP. Ver el README."
