#!/bin/bash
# Creates the staging database if it doesn't exist
set -e
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    SELECT 'CREATE DATABASE relay_staging'
    WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'relay_staging')\gexec
EOSQL
