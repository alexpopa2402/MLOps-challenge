#!/usr/bin/env bash

set -Eeuo pipefail
ENV_FILE="${1:-.challenge_env}"
NAMESPACE="flux-system"
SECRET_NAME="challenge-secrets"
require_command() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing required command: $1" >&2
    exit 1
  }
}
require_command kubectl
require_command openssl
if [[ ! -f "$ENV_FILE" ]]; then
  umask 077
  cat >"$ENV_FILE" <<EOF
KEYCLOAK_ADMIN_USERNAME=admin
KEYCLOAK_ADMIN_PASSWORD=$(openssl rand -base64 24 | tr -d '\n')
DEMO_USER_USERNAME=demo
DEMO_USER_PASSWORD=$(openssl rand -base64 18 | tr -d '\n')
DEMO_USER_EMAIL=demo@example.com
OAUTH2_PROXY_CLIENT_ID=loadtester
OAUTH2_PROXY_CLIENT_SECRET=$(openssl rand -hex 32)
OAUTH2_PROXY_COOKIE_SECRET=$(openssl rand -base64 32 | tr -d '\n')
EOF
  chmod 600 "$ENV_FILE"
  echo "Generated $ENV_FILE"
else
  echo "Reusing existing $ENV_FILE"
fi
kubectl get namespace "$NAMESPACE" >/dev/null
kubectl create secret generic "$SECRET_NAME" \
  --namespace "$NAMESPACE" \
  --from-env-file="$ENV_FILE" \
  --dry-run=client \
  -o yaml |
  kubectl apply -f -
kubectl label secret "$SECRET_NAME" \
  --namespace "$NAMESPACE" \
  reconcile.fluxcd.io/watch=Enabled \
  --overwrite
echo "Created/updated Secret $NAMESPACE/$SECRET_NAME"
echo "Credentials remain only in $ENV_FILE and the local cluster."