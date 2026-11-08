# Builds SessionBridge.exe next to this script. Needs uv (https://docs.astral.sh/uv/).
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
uvx --python 3.12 --with zstandard pyinstaller --noconfirm --onefile --windowed --name SessionBridge --collect-all zstandard --hidden-import test_bridge --paths . --distpath . --workpath build --specpath build app.py
Remove-Item build -Recurse -Force
