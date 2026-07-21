'use strict';

const express = require('express');
const multer = require('multer');

const MAX_FILE_BYTES = 20 * 1024 * 1024;

const upload = multer({
  storage: multer.memoryStorage(),
  limits: { fileSize: MAX_FILE_BYTES },
});

function ocrRouter(engine) {
  const router = express.Router();
  router.post('/ocr', upload.single('image'), async (req, res, next) => {
    if (!req.file || req.file.buffer.length === 0) {
      res.status(400).json({ error: 'missing_image' });
      return;
    }
    try {
      const result = await engine.recognizeEncoded(req.file.buffer);
      res.status(200).json({ lines: result.lines });
    } catch (error) {
      next(error);
    }
  });
  return router;
}

module.exports = ocrRouter;
