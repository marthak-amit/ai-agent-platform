# ─────────────────────────────────────────────────────────────────────────────
# Marketing site — Node 20 / Vite SSG / static
# ─────────────────────────────────────────────────────────────────────────────

FROM node:20-slim

WORKDIR /app

COPY package.json package-lock.json* ./
RUN npm ci

COPY . .
RUN npm run build

EXPOSE 3000

# `serve` statically hosts the prerendered dist/ output produced by vite-ssg.
# Railway injects $PORT at runtime; shell form lets us expand it.
CMD npx serve dist -l ${PORT:-3000}
