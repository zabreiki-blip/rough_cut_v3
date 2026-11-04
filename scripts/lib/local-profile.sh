# Shared by local setup, runner and doctor. ROOT_DIR must be set by the caller.
select_local_profile() {
  LOCAL_OS="$(uname -s)"
  LOCAL_ARCH="$(uname -m)"
  LOCAL_TRANSLATED=0
  if [[ "${LOCAL_OS}" == Darwin ]]; then
    LOCAL_TRANSLATED="$(sysctl -in sysctl.proc_translated 2>/dev/null || echo 0)"
  fi
  LOCAL_NATIVE_ARM=0
  if [[ "${LOCAL_OS}" == Darwin && "${LOCAL_ARCH}" == arm64 && "${LOCAL_TRANSLATED}" != 1 ]]; then
    LOCAL_NATIVE_ARM=1
  fi
  case "${ROUGH_CUT_PROFILE:-auto}" in
    auto) if [[ "${LOCAL_NATIVE_ARM}" == 1 ]]; then ROUGH_CUT_PROFILE=mlx; else ROUGH_CUT_PROFILE=cpu; fi ;;
    cpu|mlx) ;;
    *) echo "ROUGH_CUT_PROFILE must be auto, cpu or mlx." >&2; return 2 ;;
  esac
  if [[ "${ROUGH_CUT_PROFILE}" == mlx && "${LOCAL_NATIVE_ARM}" != 1 ]]; then
    echo "MLX requires native macOS arm64 (Rosetta is not supported). Use ROUGH_CUT_PROFILE=cpu or scripts/bootstrap-docker." >&2
    return 2
  fi
  if [[ "${ROUGH_CUT_PROFILE}" == mlx ]]; then
    VENV_DIR="${ROUGH_CUT_VENV:-${ROOT_DIR}/.venv-macos-arm64}"
  else
    VENV_DIR="${ROUGH_CUT_VENV:-${ROOT_DIR}/.venv}"
  fi
  export ROUGH_CUT_PROFILE ROUGH_CUT_VENV="${VENV_DIR}"
}

validate_local_python() {
  local python="$1" version arch
  if ! command -v "${python}" >/dev/null 2>&1; then
    echo "Python 3.11 required (native MLX also supports 3.12). Set PYTHON_BIN to a supported interpreter." >&2
    return 2
  fi
  version="$("${python}" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
  if [[ "${version}" != 3.11 && !( "${ROUGH_CUT_PROFILE}" == mlx && "${version}" == 3.12 ) ]]; then
    echo "Python ${version} unsupported: CPU needs 3.11; native MLX supports 3.11 or 3.12." >&2
    return 2
  fi
  if [[ "${ROUGH_CUT_PROFILE}" == mlx ]]; then
    arch="$("${python}" -c 'import platform; print(platform.machine())')"
    if [[ "${arch}" != arm64 ]]; then
      echo "Python is ${arch}; MLX requires native arm64 Python (no Rosetta)." >&2
      return 2
    fi
  fi
}

missing_local_environment() {
  echo "Selected ${ROUGH_CUT_PROFILE} environment: ${VENV_DIR}. Run scripts/setup-local with the same ROUGH_CUT_PROFILE/ROUGH_CUT_VENV." >&2
  if [[ -x "${ROOT_DIR}/.venv/bin/gunicorn" ]]; then
    echo "Existing CPU environment: ROUGH_CUT_PROFILE=cpu scripts/run-local" >&2
  fi
  if [[ -x "${ROOT_DIR}/.venv-macos-arm64/bin/gunicorn" ]]; then
    echo "Existing MLX environment: ROUGH_CUT_PROFILE=mlx scripts/run-local" >&2
  fi
}
