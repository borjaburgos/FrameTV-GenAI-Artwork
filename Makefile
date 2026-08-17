.PHONY: build up up-bridge down logs backup restore test e2e

build:
	docker compose build frameart-api

up:
	docker compose --profile lan up -d frameart-api-lan

up-bridge:
	docker compose up -d frameart-api

down:
	docker compose --profile lan down

logs:
	docker compose --profile lan logs -f frameart-api-lan

backup:
	docker compose --profile lan exec -T frameart-api-lan frameart data backup --output /data/frameart/backups/manual-$$(date -u +%Y%m%dT%H%M%SZ).tar.gz

restore:
	@test -n "$(BACKUP)" || (echo "Usage: make restore BACKUP=/data/frameart/backups/<file>.tar.gz" && exit 2)
	docker compose --profile lan stop frameart-api-lan
	docker compose run --rm --no-deps frameart data restore --archive "$(BACKUP)" --yes
	docker compose --profile lan up -d frameart-api-lan

test:
	pytest
	ruff check frameart/ tests/

e2e:
	npm run test:e2e
