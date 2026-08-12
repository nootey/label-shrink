FROM node:22-slim

RUN corepack enable

WORKDIR /app

COPY package.json pnpm-lock.yaml ./
RUN pnpm install --prod --frozen-lockfile

COPY app.js pdf-to-a4.sh ./
RUN chmod +x pdf-to-a4.sh

CMD ["node", "app.js"]
