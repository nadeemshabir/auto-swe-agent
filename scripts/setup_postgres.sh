#!/usr/bin/env bash
# Setup script for the auto-swe-agent PostgreSQL database.
# Run this once: bash scripts/setup_postgres.sh
set -euo pipefail

echo "=== PostgreSQL Setup for auto-swe-agent ==="
echo

# 1. Check if Postgres is running
if ! pg_isready -q; then
    echo "Postgres is not running. Starting it..."
    sudo service postgresql start
    sleep 2
fi
echo "[ok] PostgreSQL is running"

# 2. Create the database user (idempotent)
sudo -u postgres psql -tc "SELECT 1 FROM pg_roles WHERE rolname='agent'" | grep -q 1 \
    || sudo -u postgres psql -c "CREATE USER agent WITH PASSWORD 'agent';"
echo "[ok] User 'agent' exists"

# 3. Create the database (idempotent)
sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='auto_swe_agent'" | grep -q 1 \
    || sudo -u postgres psql -c "CREATE DATABASE auto_swe_agent OWNER agent;"
echo "[ok] Database 'auto_swe_agent' exists"

# 4. Grant privileges
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE auto_swe_agent TO agent;"
echo "[ok] Privileges granted"

# 5. Test the connection with the agent user
PGPASSWORD=agent psql -h localhost -U agent -d auto_swe_agent -c "SELECT 1 AS connected;" > /dev/null
echo "[ok] Connection test passed (agent@auto_swe_agent)"

echo
echo "=== Done! ==="
echo "DATABASE_URL=postgresql://agent:agent@localhost:5432/auto_swe_agent"
