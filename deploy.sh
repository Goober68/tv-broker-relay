#!/usr/bin/env bash
# deploy.sh — Deploy or update tv-broker-relay on a Ubuntu/Debian VPS
#
# First-time setup:  bash deploy.sh --setup
# Update:            bash deploy.sh
# Rollback:          bash deploy.sh --rollback
#
# Requirements:
#   - Ubuntu 22.04+ or Debian 12+
#   - Run as a non-root user with sudo access
#   - Domain DNS already pointing to this server

set -euo pipefail

# ── Config ─────────────────────────────────────────────────────────────────────
APP_DIR="${APP_DIR:-/opt/tv-broker-relay}"
REPO_URL="${REPO_URL:-}"  # set this if deploying from git: https://github.com/you/repo
DOMAIN="${DOMAIN:-}"      # your domain, used to validate Caddyfile

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()    { echo -e "${GREEN}[deploy]${NC} $*"; }
warn()    { echo -e "${YELLOW}[warn]${NC}  $*"; }
error()   { echo -e "${RED}[error]${NC} $*"; exit 1; }

# ── Preflight checks ───────────────────────────────────────────────────────────

check_env() {
    info "Checking environment..."
    [[ -f "$APP_DIR/.env" ]] || error ".env not found at $APP_DIR/.env — copy .env.example and fill it in"

    # Verify no placeholder values remain
    local dangerous=("change-me" "change_me" "yourdomain.com" "sk_test_..." "whsec_...")
    for val in "${dangerous[@]}"; do
        if grep -q "$val" "$APP_DIR/.env" 2>/dev/null; then
            error ".env still contains placeholder: '$val' — fill in all values before deploying"
        fi
    done
    info ".env looks good"
}

generate_secrets() {
    info "Generating secrets..."
    local env_file="$APP_DIR/.env"

    # Generate POSTGRES_PASSWORD if not set
    if ! grep -q "^POSTGRES_PASSWORD=" "$env_file"; then
        local pg_pass
        pg_pass=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
        echo "POSTGRES_PASSWORD=$pg_pass" >> "$env_file"
        info "Generated POSTGRES_PASSWORD"
    fi

    # Generate JWT_SECRET if still placeholder
    if grep -q "change-me-to-a-random-secret" "$env_file"; then
        local jwt_secret
        jwt_secret=$(python3 -c "import secrets; print(secrets.token_hex(32))")
        sed -i "s|JWT_SECRET=change-me-to-a-random-secret|JWT_SECRET=$jwt_secret|" "$env_file"
        info "Generated JWT_SECRET"
    fi

    # Generate CREDENTIAL_ENCRYPTION_KEY if still placeholder
    if grep -q "change-me-generate" "$env_file"; then
        # Use Python's standard library base64 to generate a valid Fernet key
        local fernet_key
        fernet_key=$(python3 -c "
import os, base64
key = base64.urlsafe_b64encode(os.urandom(32)).decode()
print(key)
")
        sed -i "s|CREDENTIAL_ENCRYPTION_KEY=.*|CREDENTIAL_ENCRYPTION_KEY=$fernet_key|" "$env_file"
        info "Generated CREDENTIAL_ENCRYPTION_KEY"
    fi
}

# ── System setup (first-time only) ────────────────────────────────────────────

install_docker() {
    if command -v docker &>/dev/null; then
        info "Docker already installed ($(docker --version))"
        return
    fi
    info "Installing Docker..."
    curl -fsSL https://get.docker.com | sh
    sudo usermod -aG docker "$USER"
    warn "Added $USER to docker group. You may need to log out and back in."
}

setup() {
    info "=== First-time setup ==="

    install_docker

    # Create app directory
    sudo mkdir -p "$APP_DIR"
    sudo chown "$USER:$USER" "$APP_DIR"

    # Copy files (assumes script is run from the project directory)
    if [[ -n "$REPO_URL" ]]; then
        git clone "$REPO_URL" "$APP_DIR" || git -C "$APP_DIR" pull
    else
        info "Copying files from current directory to $APP_DIR..."
        rsync -av --exclude='.git' --exclude='__pycache__' --exclude='.env' \
            ./ "$APP_DIR/"
    fi

    # Create .env from example if it doesn't exist
    if [[ ! -f "$APP_DIR/.env" ]]; then
        cp "$APP_DIR/.env.example" "$APP_DIR/.env"
        warn ".env created from .env.example — EDIT IT NOW before continuing"
        warn "Required: DOMAIN in Caddyfile, JWT_SECRET, CREDENTIAL_ENCRYPTION_KEY,"
        warn "  STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET, and your broker credentials"
        echo ""
        echo "Edit $APP_DIR/.env, then re-run: bash deploy.sh"
        exit 0
    fi

    generate_secrets
    check_env

    # Open firewall ports (if ufw is active)
    if command -v ufw &>/dev/null && sudo ufw status | grep -q "Status: active"; then
        info "Opening ports 80 and 443 in ufw..."
        sudo ufw allow 80/tcp
        sudo ufw allow 443/tcp
    fi

    info "Setup complete. Starting services..."
    deploy
}

# ── Deploy / Update ────────────────────────────────────────────────────────────

deploy() {
    info "=== Deploying ==="
    cd "$APP_DIR"

    # Pull latest if using git
    if [[ -n "$REPO_URL" ]] && [[ -d ".git" ]]; then
        info "Pulling latest code..."
        git pull
    fi

    check_env

    # Tag current image for rollback
    if docker image inspect tv-broker-relay-app:latest &>/dev/null 2>&1; then
        docker tag tv-broker-relay-app:latest tv-broker-relay-app:rollback || true
        info "Previous image tagged as :rollback"
    fi

    info "Building images..."
    docker compose build --pull

    info "Running database migrations..."
    docker compose run --rm migrate

    info "Starting services..."
    docker compose up -d --remove-orphans

    info "Waiting for health check..."
    local max_attempts=30
    local attempt=0
    until docker compose exec -T app curl -sf http://localhost:8000/health >/dev/null 2>&1; do
        attempt=$((attempt + 1))
        if [[ $attempt -ge $max_attempts ]]; then
            error "Health check failed after ${max_attempts} attempts. Check: docker compose logs app"
        fi
        sleep 2
    done

    info "✅ Deployment complete!"
    info "API docs: https://${DOMAIN:-yourdomain.com}/docs"
    docker compose ps
}

# ── Rollback ───────────────────────────────────────────────────────────────────

rollback() {
    info "=== Rolling back ==="
    cd "$APP_DIR"

    if ! docker image inspect tv-broker-relay-app:rollback &>/dev/null 2>&1; then
        error "No rollback image found. Cannot roll back."
    fi

    docker tag tv-broker-relay-app:rollback tv-broker-relay-app:latest
    docker compose up -d app
    info "Rolled back to previous image"
    warn "NOTE: Database migrations are NOT rolled back automatically."
    warn "If the migration caused issues, restore from a backup."
}

# ── Main ───────────────────────────────────────────────────────────────────────

case "${1:-deploy}" in
    --setup)   setup ;;
    --rollback) rollback ;;
    deploy|"") deploy ;;
    *)         error "Unknown command: $1. Use --setup, --rollback, or no argument to deploy." ;;
esac
