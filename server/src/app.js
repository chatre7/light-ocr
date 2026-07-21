'use strict';

const express = require('express');

const healthRouter = require('./routes/health');
const infoRouter = require('./routes/info');
const { errorHandler } = require('./errors');

function createApp(engine) {
  const app = express();
  app.use(healthRouter());
  app.use(infoRouter(engine));
  app.use(errorHandler);
  return app;
}

module.exports = { createApp };
