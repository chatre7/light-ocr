FROM node:22-trixie-slim

WORKDIR /app

COPY server/package.json server/package-lock.json ./
RUN npm ci --omit=dev

COPY server/src ./src

RUN groupadd -r ocr && useradd -r -g ocr ocr
USER ocr

EXPOSE 3000
ENV EXECUTION_MODE=cpu
ENV QUEUE_CAPACITY=4

CMD ["node", "src/server.js"]
