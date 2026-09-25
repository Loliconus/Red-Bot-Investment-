#!/usr/bin/env sh
# Development only. Precompiled CSS is committed; Node is NOT a runtime dependency.
set -eu
cd "$(dirname "$0")/.."
if [ ! -x adapters/driving/web/assets/node_modules/.bin/tailwindcss ]; then
  npm ci --prefix adapters/driving/web/assets
fi
adapters/driving/web/assets/node_modules/.bin/tailwindcss \
  -c adapters/driving/web/assets/tailwind.config.cjs \
  -i adapters/driving/web/assets/css/input.css \
  -o adapters/driving/web/static/css/app.css --minify
