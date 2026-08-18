FROM node:22-slim

RUN corepack enable \
 && apt-get update \
 && apt-get install -y --no-install-recommends python3 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY package.json pnpm-lock.yaml ./
RUN pnpm install --prod --frozen-lockfile

COPY app.js pdf_to_a4.py ./

CMD ["node", "app.js"]
