# ─────────────────────────────────────────────────────────────────────────────
# Frontend — Node 20 / Vite / React
# ─────────────────────────────────────────────────────────────────────────────

FROM node:20-slim

WORKDIR /app

# Install dependencies first (own layer — cached unless package.json changes).
COPY package.json package-lock.json ./
RUN npm ci

# Copy source.
# In development the src/ directory is overridden by a volume mount; this
# layer is mainly used for the production / CI build path.
COPY . .

EXPOSE 3000

# --host 0.0.0.0  → listen on all interfaces so Docker port mapping works.
# --port 3000     → override Vite's default 5173.
# VITE_BACKEND_URL is read by vite.config.ts to set the proxy target.
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0", "--port", "3000"]
