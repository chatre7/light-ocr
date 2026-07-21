# REST API + Docker for light-ocr — Design

## Goal

Expose the existing `@arcships/light-ocr` OCR engine over HTTP so it can be called
from any language/environment, and package it as a Docker image for easy
deployment. This is additive: it does not change `bindings/node` or the C++
core.

## Non-goals

- No changes to the native addon, CMake build, or model bundling.
- No async job-queue API (out of scope for v1; the engine's own
  `queueCapacity` provides backpressure for a synchronous API).
- No GPU-specific Docker image variant — a single image supports both CPU and
  WebGPU via a runtime env var, since the Linux x64 glibc npm package already
  ships a WebGPU-capable native binary.

## Architecture

New top-level directory `server/`, a standalone Node.js/Express app that
depends on the published `@arcships/light-ocr` npm package (not the in-repo
`bindings/node` source — no C++ compilation needed to build or run it).

```
server/
├── package.json          # express, multer, @arcships/light-ocr
├── src/
│   ├── server.js         # Express app entry point, graceful shutdown
│   ├── engine.js         # createEngine() wrapper, startup init
│   ├── routes/
│   │   ├── ocr.js        # POST /ocr
│   │   ├── health.js     # GET /health
│   │   └── info.js       # GET /info
│   └── errors.js         # OcrError -> HTTP status mapping
├── test/
│   └── ocr.test.js       # node --test end-to-end tests
├── Dockerfile
└── .dockerignore
```

Design decisions:
- One `engine` instance is created at process startup and shared across all
  requests; `queueCapacity` bounds in-flight + queued recognition calls.
  Express does not add its own queue on top.
- Routes are thin handlers; engine lifecycle and recognition logic live in
  `engine.js`.

## Endpoints

### `GET /health`
```json
200 OK
{ "status": "ok" }
```
Lightweight liveness check — confirms the engine finished initializing.

### `GET /info`
```json
200 OK
{
  "execution": { "provider": "cpu", "sessions": { ... } },
  "version": "0.3.0"
}
```
Returns `engine.info.execution` verbatim plus the server's own version, useful
for confirming which provider (cpu/webgpu) is actually active in a given
container.

### `POST /ocr`
`multipart/form-data`, file field name `image`.

```bash
curl -F "image=@sample.jpg" http://localhost:3000/ocr
```

Success:
```json
200 OK
{
  "lines": [
    { "text": "HELLO 123", "confidence": 0.98, "box": [[x, y], ...] }
  ]
}
```

Validation:
- Missing/empty `image` field → `400 Bad Request`
- File exceeds size limit (default 20MB, via `multer` `limits.fileSize`) →
  `413 Payload Too Large`
- Data that fails JPEG/PNG decode in `recognizeEncoded` → `422 Unprocessable
  Entity`

## Execution mode

Controlled by an env var at container run time, not baked into the image
build — the same image works for both CPU and WebGPU:

```bash
# CPU only (default)
docker run -p 3000:3000 light-ocr-api

# WebGPU (host must expose the GPU/driver to the container, e.g. --gpus all)
docker run -p 3000:3000 -e EXECUTION_MODE=auto --gpus all light-ocr-api
```

```js
const provider = process.env.EXECUTION_MODE ?? 'cpu'; // 'cpu' | 'auto' | 'webgpu'
const engine = await createEngine({
  queueCapacity: Number(process.env.QUEUE_CAPACITY ?? 4),
  execution: { provider },
});
```

Default is `cpu` so the image runs anywhere without depending on GPU drivers
being present.

## Error handling

`errors.js` maps `OcrError.code` to HTTP status:

| OcrError code | HTTP status |
|---|---|
| `queue_full` | 429 Too Many Requests |
| `resource_limit_exceeded` | 413 Payload Too Large |
| `invalid_argument` | 400 Bad Request |
| other/unexpected | 500 Internal Server Error |

## Graceful shutdown

```js
process.on('SIGTERM', async () => {
  server.close();        // stop accepting new connections
  await engine.close();  // drain in-flight/queued requests (FIFO)
  process.exit(0);
});
```
Required because `docker stop` sends `SIGTERM`; without this the engine could
be killed mid-request.

## Dockerfile

Single-stage — no compilation needed since the npm package ships prebuilt
native binaries + model bundle for the target platform:

```dockerfile
FROM node:22-slim

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
```

- `node:22-slim` matches `engines.node: "^22.0.0 || ^24.0.0"` and is
  glibc-based, matching the target platform of the prebuilt native package.
- Runs as non-root user `ocr`.
- `.dockerignore` excludes `node_modules`, `test/`, `*.md`.

Optional `docker-compose.yml` for local dev convenience:
```yaml
services:
  light-ocr-api:
    build:
      context: .
      dockerfile: server/Dockerfile
    ports:
      - "3000:3000"
    environment:
      - EXECUTION_MODE=cpu
```

## Testing plan

- `server/test/ocr.test.js` using `node --test` (matches the convention in
  `bindings/node/test/`).
- POST /ocr with a real sample image (reuse a `corpus/fixtures` image if
  suitable) and assert the response contains sensible `lines`.
- Validation tests: missing file → 400, non-image data → 422.
- /health and /info return 200 with expected fields.
- Manual verification: `docker build` + `docker run` + `curl` against the
  running container, not just unit tests on the dev machine.
