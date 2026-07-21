'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { after, before, test } = require('node:test');

const { initEngine } = require('../src/engine');
const { createApp } = require('../src/app');

let engine;
let server;
let baseUrl;

before(async () => {
  engine = await initEngine();
  const app = createApp(engine);
  server = app.listen(0);
  await new Promise((resolve) => server.once('listening', resolve));
  baseUrl = `http://127.0.0.1:${server.address().port}`;
});

after(async () => {
  await new Promise((resolve) => server.close(resolve));
  await engine.close();
});

test('GET /health returns 200 ok', async () => {
  const response = await fetch(`${baseUrl}/health`);
  assert.equal(response.status, 200);
  const body = await response.json();
  assert.deepEqual(body, { status: 'ok' });
});

test('GET /info returns execution info and version', async () => {
  const response = await fetch(`${baseUrl}/info`);
  assert.equal(response.status, 200);
  const body = await response.json();
  assert.equal(body.execution.requestedProvider, 'cpu');
  assert.equal(typeof body.version, 'string');
});

test('POST /ocr recognizes text in a real image', async () => {
  const imagePath = path.resolve(__dirname, '../../docs/assets/benchmark-generated-hello-123.png');
  const imageBuffer = fs.readFileSync(imagePath);
  const form = new FormData();
  form.set('image', new Blob([imageBuffer], { type: 'image/png' }), 'hello-123.png');

  const response = await fetch(`${baseUrl}/ocr`, { method: 'POST', body: form });
  assert.equal(response.status, 200);
  const body = await response.json();
  assert.ok(Array.isArray(body.lines));
  assert.ok(body.lines.some((line) => /HELLO/i.test(line.text)));
});

test('POST /ocr without a file returns 400', async () => {
  const form = new FormData();
  const response = await fetch(`${baseUrl}/ocr`, { method: 'POST', body: form });
  assert.equal(response.status, 400);
  const body = await response.json();
  assert.equal(body.error, 'missing_image');
});

test('POST /ocr with non-image data returns 422', async () => {
  const form = new FormData();
  form.set('image', new Blob([Buffer.from('not an image')], { type: 'application/octet-stream' }), 'garbage.bin');

  const response = await fetch(`${baseUrl}/ocr`, { method: 'POST', body: form });
  assert.equal(response.status, 422);
});
