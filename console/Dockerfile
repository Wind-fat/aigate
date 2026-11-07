# Console image. Two stages: build with node, run with node (standalone output).
#
# Build:   docker build -t aigate-console:0.3.0 ./console
# Run:     docker run -p 3000:3000 -e AIGATE_API_BASE=http://gateway:8080 aigate-console:0.3.0
#
# Notes:
# - The old file had a third "deps" stage that nothing copied from — it installed
#   node_modules twice and then threw the first copy away. Removed; `npm ci` in the build
#   stage is cached on package-lock.json alone, which is what that stage was trying to do.
# - `output: "standalone"` (next.config.ts) is what keeps the runtime layer small: it copies
#   the trimmed node_modules and a server.js, not the whole repo.
# - The admin token lives in this server process only (`AIGATE_ADMIN_TOKEN`), never in the
#   browser bundle — that is why the console proxies /api/admin instead of talking to the
#   gateway from the client.
FROM node:22-alpine AS build
WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY . .
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build

FROM node:22-alpine AS runtime
WORKDIR /app

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0

RUN addgroup -g 10001 -S nodejs && adduser -u 10001 -S nextjs -G nodejs

COPY --from=build --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=build --chown=nextjs:nodejs /app/.next/static ./.next/static
COPY --from=build --chown=nextjs:nodejs /app/public ./public

USER nextjs
EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD node -e "fetch('http://127.0.0.1:3000/login').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

CMD ["node", "server.js"]