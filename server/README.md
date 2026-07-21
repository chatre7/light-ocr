# light-ocr REST API

HTTP wrapper around the `@arcships/light-ocr` OCR engine.

## Run locally

    cd server
    npm install
    npm start

Server listens on `PORT` (default `3000`).

## Run with Docker

    docker build -f server/Dockerfile -t light-ocr-api .
    docker run --rm -p 3000:3000 light-ocr-api

Or with Docker Compose (from the repo root):

    docker compose up --build

## Endpoints

- `GET /health` - liveness check, returns `{ "status": "ok" }`
- `GET /info` - current engine execution info and server version
- `POST /ocr` - `multipart/form-data` with a file field named `image` (JPEG or PNG, up to 20MB)

Example:

    curl -F "image=@sample.jpg" http://localhost:3000/ocr

## Environment variables

| Variable          | Default | Description                                     |
| ----------------- | ------- | ------------------------------------------------ |
| `PORT`             | `3000`  | HTTP port                                        |
| `EXECUTION_MODE`   | `cpu`   | `cpu`, `auto`, or `webgpu`                       |
| `QUEUE_CAPACITY`   | `4`     | Max concurrent + queued recognition requests     |

WebGPU requires the host to expose a compatible GPU/driver to the container
(e.g. `docker run --gpus all`); the same image works for both modes.
