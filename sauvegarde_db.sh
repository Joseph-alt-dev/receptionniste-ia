#!/usr/bin/env bash
# Sauvegarde salons.db dans backups/ (jamais committé, voir .gitignore).
# Usage : ./sauvegarde_db.sh

set -euo pipefail
cd "$(dirname "$0")"

mkdir -p backups
destination="backups/salons-$(date +%Y-%m-%d-%H%M).db"
cp salons.db "$destination"
echo "Sauvegarde créée : $destination"
