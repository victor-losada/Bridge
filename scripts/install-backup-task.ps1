<#
.SYNOPSIS
    Programa la copia de seguridad diaria de la configuracion del Bridge.

.DESCRIPTION
    Copia .env y data\slots.json. Son unos pocos KB, asi que conservar dos
    semanas no cuesta nada y da margen para darse cuenta de un error.

    OJO: la copia lleva la FERNET_KEY en claro. -Destino debe ser una carpeta
    local del servidor, nunca una unidad de red ni una carpeta sincronizada.

.EXAMPLE
    .\scripts\install-backup-task.ps1
    .\scripts\install-backup-task.ps1 -Destino D:\copias -Hora 04:30 -Conservar 30
#>

param(
    [string] $Nombre    = "Bridge Backup",
    [string] $Destino   = "D:\copias-bridge",
    [string] $Hora      = "03:00",
    [int]    $Conservar = 14,
    [switch] $Desinstalar
)

$ErrorActionPreference = "Stop"

$raiz   = Split-Path -Parent $PSScriptRoot
$python = Join-Path $raiz ".venv\Scripts\python.exe"
$quien  = "$env:USERDOMAIN\$env:USERNAME"

if (Get-ScheduledTask -TaskName $Nombre -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $Nombre -Confirm:$false
    Write-Host "Tarea anterior '$Nombre' eliminada."
}
if ($Desinstalar) {
    Write-Host "Listo. Ya no se hacen copias automaticas."
    return
}

if (-not (Test-Path $python)) { throw "No existe $python" }
New-Item -ItemType Directory -Force -Path $Destino | Out-Null

$accion = New-ScheduledTaskAction `
    -Execute $python `
    -Argument "-m app.tools.backup --dest `"$Destino`" --keep $Conservar" `
    -WorkingDirectory $raiz

$disparador = New-ScheduledTaskTrigger -Daily -At $Hora

# LogonType S4U: la tarea corre aunque no haya nadie con sesion abierta y sin
# guardar la contrasena en ningun sitio. Aqui si vale, porque el backup no
# abre ninguna ventana (a diferencia del Bridge, que lanza terminales MT5).
$principal = New-ScheduledTaskPrincipal -UserId $quien -LogonType S4U -RunLevel Highest

$ajustes = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

Register-ScheduledTask -TaskName $Nombre `
    -Action $accion -Trigger $disparador -Principal $principal -Settings $ajustes `
    -Description "Copia diaria de .env y slots.json en $Destino (conserva $Conservar)." | Out-Null

Write-Host ""
Write-Host "Tarea '$Nombre' registrada: cada dia a las $Hora -> $Destino"
Write-Host "Se conservan las $Conservar copias mas recientes."
Write-Host ""
Write-Host "  Probarla ya:  Start-ScheduledTask -TaskName '$Nombre'"
Write-Host "  Ver copias:   Get-ChildItem $Destino"
Write-Host ""
Write-Host "Esto NO sustituye tener la FERNET_KEY en tu gestor de contrasenas:"
Write-Host "si se pierde el servidor, se pierden tambien sus copias locales."
