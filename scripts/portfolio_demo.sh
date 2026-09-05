#!/usr/bin/env bash
set -euo pipefail

script_path="${BASH_SOURCE[0]}"
if [[ "${script_path}" != /* ]]; then
  script_path="${PWD}/${script_path}"
fi
script_dir="$(cd -- "${script_path%/*}" && pwd -P)"
repo_root="$(cd -- "${script_dir}/.." && pwd -P)"
python_executable="${PYTHON_PATH:-${repo_root}/.venv/bin/python}"

if [[ ! -x "${python_executable}" ]]; then
  printf 'Python executable not found: %s\n' "${python_executable}" >&2
  exit 1
fi

exec "${python_executable}" "${repo_root}/scripts/portfolio_demo.py" "$@"
