# Docker bootstrap fallback for hosts with no Python. Never source .env.
initialize_docker_env() (
  token=""
  temporary=""
  input="${ROOT_DIR}/.env"
  trap 'if [[ -n "${temporary}" ]]; then rm -f "${temporary}"; fi' EXIT
  if [[ ! -f "${input}" ]]; then
    input="${ROOT_DIR}/.env.example"
  fi
  # An empty/absent (including quoted empty) token needs repair. Read only
  # this assignment as data; shell expansion/command substitution never run.
  if awk '
    /^[ \t]*(export[ \t]+)?ROUGH_CUT_API_TOKEN[ \t]*=/ {
      value=$0; sub(/^[^=]*=/, "", value); gsub(/^[ \t]+|[ \t]+$/, "", value)
      present=1
      filled=(value != "" && value !~ /^#/ && value !~ /^"[ \t]*"[ \t]*(#.*)?$/ && value !~ /^\047[ \t]*\047[ \t]*(#.*)?$/)
    }
    END {exit (present && filled) ? 0 : 1}
  ' "${input}"; then
    return 0
  fi
  if ! command -v openssl >/dev/null 2>&1; then
    echo "Python or OpenSSL is required to generate an API token for Docker." >&2
    return 2
  fi
  token="$(openssl rand -hex 32)"
  if [[ ! "${token}" =~ ^[0-9a-f]{64}$ ]]; then
    echo "Secure API token generation failed." >&2
    return 2
  fi
  temporary="$(mktemp "${ROOT_DIR}/.env.XXXXXX")"
  if [[ -f "${ROOT_DIR}/.env" ]]; then
    cp -p "${ROOT_DIR}/.env" "${temporary}"
  fi
  awk -v token="${token}" '
    /^[ \t]*(export[ \t]+)?ROUGH_CUT_API_TOKEN[ \t]*=/ {
      value=$0; sub(/^[^=]*=/, "", value); gsub(/^[ \t]+|[ \t]+$/, "", value)
      if (value == "" || value ~ /^#/ || value ~ /^"[ \t]*"[ \t]*(#.*)?$/ || value ~ /^\047[ \t]*\047[ \t]*(#.*)?$/) {
        sub(/=.*/, "=" token)
      }
      present=1
    }
    {print}
    END {if (!present) print "ROUGH_CUT_API_TOKEN=" token}
  ' "${input}" > "${temporary}"
  mv -f "${temporary}" "${ROOT_DIR}/.env"
)
