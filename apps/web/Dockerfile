FROM node:22-bookworm-slim AS build
ENV NEXT_TELEMETRY_DISABLED=1
WORKDIR /app

COPY apps/web/package.json apps/web/package-lock.json ./
RUN npm ci
COPY apps/web/next.config.ts apps/web/tsconfig.json apps/web/next-env.d.ts ./
COPY apps/web/app ./app
COPY apps/web/components ./components
COPY apps/web/lib ./lib
RUN npm run build

FROM node:22-bookworm-slim AS runtime
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1
WORKDIR /app
COPY --from=build --chown=node:node /app/package.json /app/package-lock.json ./
COPY --from=build --chown=node:node /app/next.config.ts ./
COPY --from=build --chown=node:node /app/.next ./.next
COPY --from=build --chown=node:node /app/node_modules ./node_modules
USER node
EXPOSE 3000

# Invoke Next directly so the local npm script's loopback flag cannot override
# the container listener. CONVERSATION_URL is server-side runtime configuration.
CMD ["node", "node_modules/next/dist/bin/next", "start", "--hostname", "0.0.0.0", "--port", "3000"]
