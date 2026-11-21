param([string]$Python = 'python')

$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $project
try {
    $buildRoot = Join-Path $project '.solver-deps/gui-build'
    $database = Join-Path $project 'solver/steam_database.json'
    $references = Join-Path $project 'solver/reference_layouts.json'
    New-Item -ItemType Directory -Path $buildRoot -Force | Out-Null
    & $Python -m PyInstaller --onefile --windowed --noupx --noconfirm `
        --name JiangCityBlueprint --workpath (Join-Path $buildRoot 'work') --specpath $buildRoot `
        --distpath (Join-Path $project 'dist') --paths $project `
        --add-data "${database}:solver" --add-data "${references}:solver" launch_gui.pyw
    if ($LASTEXITCODE) { throw 'Windows GUI 打包失败' }
} finally {
    Pop-Location
}
