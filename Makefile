# ===========================================
# Pi5 Homelab - Makefile
# ===========================================

COMPOSE := docker compose -f ~/homelab/docker-compose.yaml

.PHONY: help up down restart logs status pull clean

help:
	@echo "Homelab Commands:"
	@echo ""
	@echo "  Core:"
	@echo "    make up          - Start core services"
	@echo "    make down        - Stop all services"
	@echo "    make restart     - Restart all services"
	@echo "    make status      - Show container status"
	@echo ""
	@echo "  Media Stack:"
	@echo "    make up-media    - Start core + media services (with VPN)"
	@echo "    make down-media  - Stop media services only"
	@echo "    make vpn-status  - Check VPN connection status"
	@echo "    make vpn-ip      - Show VPN public IP"
	@echo ""
	@echo "  Photos (Immich):"
	@echo "    make up-photos   - Start Immich photo management"
	@echo "    make down-photos - Stop Immich services"
	@echo ""
	@echo "  Matrix Chat:"
	@echo "    make up-matrix   - Start Matrix chat server"
	@echo "    make down-matrix - Stop Matrix services"
	@echo "    make matrix-init - Initialize Matrix (first time setup)"
	@echo ""
	@echo "  Jellyfin:"
	@echo "    make jellyfin-scan - Trigger library scan"
	@echo ""
	@echo "  Logs:"
	@echo "    make logs        - Follow all logs"
	@echo "    make logs-X      - Follow logs for service X"
	@echo ""
	@echo "  Maintenance:"
	@echo "    make pull        - Pull latest images"
	@echo "    make clean       - Remove stopped containers"

# Core services
up:
	$(COMPOSE) up -d

down:
	$(COMPOSE) down

restart:
	$(COMPOSE) restart

status:
	$(COMPOSE) ps -a

# Media stack
up-media:
	$(COMPOSE) --profile media up -d

down-media:
	$(COMPOSE) --profile media stop gluetun qbittorrent prowlarr sonarr radarr bazarr lidarr jellyfin flaresolverr

# Photos stack (Immich)
up-photos:
	$(COMPOSE) --profile photos up -d

down-photos:
	$(COMPOSE) --profile photos stop immich-server immich-machine-learning immich-redis immich-postgres

# Matrix chat
up-matrix:
	$(COMPOSE) --profile matrix up -d

down-matrix:
	$(COMPOSE) --profile matrix stop synapse matrix-postgres element

matrix-init:
	@echo "Generating Matrix homeserver config..."
	docker run --rm -v ~/homelab/matrix/synapse:/data \
		-e SYNAPSE_SERVER_NAME=matrix.thopi.ts \
		-e SYNAPSE_REPORT_STATS=no \
		matrixdotorg/synapse:latest generate
	@echo ""
	@echo "Config generated! Now edit ~/homelab/matrix/synapse/homeserver.yaml to:"
	@echo "1. Set database to PostgreSQL (see below)"
	@echo "2. Enable registration if desired"
	@echo ""
	@echo "Then run: make up-matrix"

matrix-user:
	@read -p "Enter username: " user; \
	docker exec -it synapse register_new_matrix_user http://localhost:8008 -c /data/homeserver.yaml -u $$user -a

# Start everything
up-all:
	$(COMPOSE) --profile media --profile photos --profile matrix up -d

# Jellyfin commands
jellyfin-scan:
	@echo "Triggering Jellyfin library scan..."
	@curl -s -X POST "http://localhost:8096/Library/Refresh" \
		-H "X-Emby-Token: $${JELLYFIN_API_KEY:-$$(grep HOMEPAGE_VAR_JELLYFIN_KEY ~/homelab/.env 2>/dev/null | cut -d= -f2)}" \
		&& echo "✓ Library scan started" || echo "✗ Failed"

# VPN commands
vpn-status:
	@docker exec gluetun /gluetun-entrypoint healthcheck && echo "VPN is UP" || echo "VPN is DOWN"

vpn-ip:
	@echo "VPN Public IP:"
	@docker exec gluetun wget -qO- https://ipinfo.io/ip && echo ""

# Logs
logs:
	$(COMPOSE) logs -f

logs-%:
	$(COMPOSE) logs -f $*

# Maintenance
pull:
	$(COMPOSE) pull

pull-all:
	$(COMPOSE) --profile media --profile photos --profile matrix pull

clean:
	docker system prune -f

# URLs
urls:
	@echo "Service URLs:"
	@echo ""
	@echo "  Core:"
	@echo "    Dashboard:    http://home.thopi.ts"
	@echo "    AdGuard:      http://adguard.thopi.ts"
	@echo "    Portainer:    http://portainer.thopi.ts"
	@echo "    Uptime Kuma:  http://uptime.thopi.ts"
	@echo ""
	@echo "  Media:"
	@echo "    Jellyfin:     http://jellyfin.thopi.ts"
	@echo "    Sonarr:       http://sonarr.thopi.ts"
	@echo "    Radarr:       http://radarr.thopi.ts"
	@echo "    qBittorrent:  http://qbit.thopi.ts"
	@echo ""
	@echo "  Photos:"
	@echo "    Immich:       http://photos.thopi.ts"
	@echo ""
	@echo "  Chat:"
	@echo "    Matrix:       http://matrix.thopi.ts"
	@echo "    Element:      http://chat.thopi.ts"
