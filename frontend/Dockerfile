# Stage 1: Build Next.js standalone output
FROM node:24-alpine AS build

ARG PACKAGE_REFRESH=manual
RUN : "$PACKAGE_REFRESH" && apk upgrade --no-cache

WORKDIR /app

COPY package.json package-lock.json* ./
RUN npm ci

COPY tsconfig.json next.config.ts ./
COPY src/ ./src/

RUN npm run build

# Stage 2: Production runtime
FROM node:24-alpine AS runner

ARG PACKAGE_REFRESH=manual
RUN : "$PACKAGE_REFRESH" && apk upgrade --no-cache

WORKDIR /app

ENV NODE_ENV=production
ENV PORT=3000

COPY --from=build /app/.next/standalone ./
COPY --from=build /app/.next/static ./.next/static

EXPOSE 3000

CMD ["node", "server.js"]
