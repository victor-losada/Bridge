<#
.SYNOPSIS
    Arranca el Bridge Manager y deja su salida en un log diario.

.DESCRIPTION
    Este es el script que ejecuta la tarea programada, no el que lanzas tu a
    mano. Se apoya en su propia ubicacion para encontrar la raiz del proyecto,
    asi que funciona igual desde D:\Bridge que desde cualquier otra ruta.

    Llama al python del entorno virtual directamente en vez de "activarlo":
    Activate.ps1 solo existe para sesiones interactivas y en una tarea
    programada anade un punto de fallo (politica de ejecucion) sin aportar nada.
#>

$ErrorActionPreference = "Stop"

$raiz   = Split-Path -Parent $PSScriptRoot
$python = Join-Path $raiz ".venv\Scripts\python.exe"
$logs   = Join-Path $raiz "logs"

if (-not (Test-Path $python)) {
    throw "No existe $python. Crea el entorno con: python -m venv .venv"
}
if (-not (Test-Path (Join-Path $raiz ".env"))) {
    throw "No existe $raiz\.env. Copialo de .env.example y pon la FERNET_KEY."
}

New-Item -ItemType Directory -Force -Path $logs | Out-Null
$log = Join-Path $logs ("bridge-" + (Get-Date -Format "yyyyMMdd") + ".log")

# El puerto y el host salen del .env; uvicorn los toma de app.main.
Set-Location $raiz
"=== arranque $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" | Out-File -Append -Encoding utf8 $log

# 2>&1 mete los errores en el mismo log: si el proceso muere al arrancar, el
# motivo queda escrito. Sin esto la tarea programada falla en silencio.
& $python -m uvicorn app.main:app --host 0.0.0.0 --port 8088 *>&1 |
    Tee-Object -Append -FilePath $log

exit $LASTEXITCODE
