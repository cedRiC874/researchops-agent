#!/usr/bin/env bash
set -euo pipefail

# Called by devcontainers/ci after the configured postCreateCommand succeeds.
receipt_dir='artifacts/ci_devcontainer_receipt'
test ! -e "${receipt_dir}"
mkdir -p "${receipt_dir}"
phase='environment'
demo_exit_code='null'
record_exit() {
  local actual_exit_code=$?
  printf '{"phase":"%s","actual_exit_code":%d,"demo_actual_exit_code":%s}\n' \
    "${phase}" "${actual_exit_code}" "${demo_exit_code}" \
    > "${receipt_dir}/process.json"
  exit "${actual_exit_code}"
}
trap record_exit EXIT

test "$(uname -m)" = 'x86_64'
test "$(id -u)" -ne 0
test -w .
git rev-parse HEAD > "${receipt_dir}/source-commit.txt"
sha256sum .devcontainer/Dockerfile .devcontainer/devcontainer.json \
  .devcontainer/setup-offline.sh .devcontainer/ci-offline-demo.sh \
  .github/workflows/devcontainer-offline-demo.yml requirements.linux.lock \
  scripts/portfolio_demo.py scripts/portfolio_demo.sh scripts/verify_phase5_artifacts.py \
  > "${receipt_dir}/input-sha256.txt"
"${PYTHON_PATH}" -c 'import json, os, platform, sys, unicodedata; print(json.dumps({"python": sys.version, "unicode": unicodedata.unidata_version, "system": platform.system(), "machine": platform.machine(), "uid": os.getuid()}))' \
  > "${receipt_dir}/environment.json"
"${PYTHON_PATH}" -m pip freeze --all > "${receipt_dir}/installed-packages.txt"
"${PYTHON_PATH}" -m pip check

phase='offline_demo'
set +e
bash scripts/portfolio_demo.sh --output-dir artifacts/ci_devcontainer_demo \
  2>&1 | tee "${receipt_dir}/demo.log"
pipeline_status=("${PIPESTATUS[@]}")
set -e
demo_exit_code="${pipeline_status[0]}"
if [[ "${demo_exit_code}" -ne 0 ]]; then
  exit "${demo_exit_code}"
fi
if [[ "${pipeline_status[1]}" -ne 0 ]]; then
  exit "${pipeline_status[1]}"
fi
phase='completed'
