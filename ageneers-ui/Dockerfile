# ageneers Pipeline Console — React + Vite frontend
#
# Build:
#   docker build -t ageneers-frontend .
#
# Run (standalone):
#   docker run --rm -p 5173:80 \
#     -e VITE_API_BASE_URL=http://localhost:8000 \
#     ageneers-frontend
#
# Note: Vite env vars (VITE_*) are baked in at BUILD time, not runtime.
# If you need a different backend URL per environment, pass
# --build-arg VITE_API_BASE_URL=... at build time, or rebuild the image.

# ── Stage 1: build static assets ─────────────────────────────────────────────
FROM node:20-slim AS build

WORKDIR /app

COPY package.json package-lock.json* ./
RUN npm ci

COPY . .

ARG VITE_API_BASE_URL=http://localhost:8000
ARG VITE_API_KEY=
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL} \
    VITE_API_KEY=${VITE_API_KEY}

RUN npm run build

# ── Stage 2: serve with nginx ────────────────────────────────────────────────
FROM nginx:1.27-alpine

COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD wget -qO- http://localhost:80/ > /dev/null || exit 1

CMD ["nginx", "-g", "daemon off;"]