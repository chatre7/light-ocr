'use strict';

const express = require('express');

function healthRouter() {
  const router = express.Router();
  router.get('/health', (req, res) => {
    res.status(200).json({ status: 'ok' });
  });
  return router;
}

module.exports = healthRouter;
