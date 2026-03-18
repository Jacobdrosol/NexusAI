#!/usr/bin/env bash
set -euo pipefail

RAW_TOOLCHAINS="${1:-${NEXUSAI_REPO_RUNTIME_TOOLCHAINS:-}}"
DOTNET_CHANNEL="${NEXUSAI_REPO_RUNTIME_DOTNET_CHANNEL:-8.0}"

if [[ -z "${RAW_TOOLCHAINS// }" ]]; then
  echo "No repo runtime toolchains requested."
  exit 0
fi

if [[ "${RAW_TOOLCHAINS,,}" == "all" ]]; then
  RAW_TOOLCHAINS="node,dotnet,go,rust,cpp"
fi

declare -A REQUESTED=()
IFS=',' read -r -a PARTS <<< "${RAW_TOOLCHAINS}"
for part in "${PARTS[@]}"; do
  token="$(echo "${part}" | tr '[:upper:]' '[:lower:]' | xargs)"
  if [[ -n "${token}" ]]; then
    REQUESTED["${token}"]=1
  fi
done

if [[ ${#REQUESTED[@]} -eq 0 ]]; then
  echo "No valid repo runtime toolchains requested."
  exit 0
fi

export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install -y --no-install-recommends ca-certificates curl gnupg

install_node() {
  echo "Installing Node.js toolchain..."
  apt-get install -y --no-install-recommends nodejs npm
  npm install -g pnpm yarn
}

install_dotnet() {
  echo "Installing .NET SDK ${DOTNET_CHANNEL}..."
  local version_id
  version_id="$(. /etc/os-release && echo "${VERSION_ID}")"
  curl -fsSL -o /tmp/packages-microsoft-prod.deb \
    "https://packages.microsoft.com/config/debian/${version_id}/packages-microsoft-prod.deb"
  dpkg -i /tmp/packages-microsoft-prod.deb
  rm -f /tmp/packages-microsoft-prod.deb
  apt-get update
  apt-get install -y --no-install-recommends "dotnet-sdk-${DOTNET_CHANNEL}"
}

install_go() {
  echo "Installing Go toolchain..."
  apt-get install -y --no-install-recommends golang-go
}

install_rust() {
  echo "Installing Rust toolchain..."
  apt-get install -y --no-install-recommends cargo rustc
}

install_cpp() {
  echo "Installing C/C++ build toolchain..."
  apt-get install -y --no-install-recommends build-essential cmake ninja-build
}

if [[ -n "${REQUESTED[node]:-}" ]]; then
  install_node
fi
if [[ -n "${REQUESTED[dotnet]:-}" ]]; then
  install_dotnet
fi
if [[ -n "${REQUESTED[go]:-}" ]]; then
  install_go
fi
if [[ -n "${REQUESTED[rust]:-}" ]]; then
  install_rust
fi
if [[ -n "${REQUESTED[cpp]:-}" ]]; then
  install_cpp
fi

rm -rf /var/lib/apt/lists/*

echo "Installed repo runtime toolchains: ${RAW_TOOLCHAINS}"
