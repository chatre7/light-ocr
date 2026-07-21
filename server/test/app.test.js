'use strict';

const assert = require('node:assert/strict');
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
