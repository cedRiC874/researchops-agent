#!/usr/bin/env bash
set -euo pipefail

# Environment preparation only: no demo, tests, Provider, services or online grants.
# Creating a codespace will download locked packages; creation/build is a separate action.
test "$(uname -m)" = "x86_64"
python -c 'import sys, unicodedata; assert sys.version_info[:3] == (3, 12, 15); assert unicodedata.unidata_version == "15.0.0"'

# Do not overwrite a developer's host-mounted project .venv.
env_dir='/home/vscode/.venvs/researchops'
if [[ ! -e "${env_dir}" ]]; then
  python -m venv "${env_dir}"
fi
test -x "${env_dir}/bin/python"
"${env_dir}/bin/python" -c 'import sys; assert sys.version_info[:3] == (3, 12, 15)'
"${env_dir}/bin/python" -m pip install -r requirements.linux.lock
"${env_dir}/bin/python" -m pip check
printf '%s\n' 'Environment prepared. Run bash scripts/portfolio_demo.sh only when you intend to run the offline deterministic demo.'
