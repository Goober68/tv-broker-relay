git pull
docker compose build --no-cache app
docker compose up -d
docker compose logs -f --tail 20 app
