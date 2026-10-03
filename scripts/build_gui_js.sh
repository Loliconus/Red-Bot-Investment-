#!/usr/bin/env sh
# Development only. Production serves committed static assets without Node.
set -eu
cd "$(dirname "$0")/.."
assets=adapters/driving/web/assets
vendor=adapters/driving/web/static/vendor
if [ ! -x "$assets/node_modules/.bin/esbuild" ]; then
  npm ci --prefix "$assets"
fi
mkdir -p "$vendor" adapters/driving/web/static/js
cp "$assets/node_modules/htmx.org/dist/htmx.min.js" "$vendor/htmx.min.js"
cp "$assets/node_modules/alpinejs/dist/cdn.min.js" "$vendor/alpine.min.js"
cp "$assets/node_modules/lightweight-charts/dist/lightweight-charts.standalone.production.js" "$vendor/lightweight-charts.js"
"$assets/node_modules/.bin/esbuild" "$assets/js/sql-editor.js" --bundle --minify \
  --format=iife --target=es2020 --outfile=adapters/driving/web/static/js/sql-editor.js
