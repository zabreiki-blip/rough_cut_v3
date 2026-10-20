/* eslint-env node */
"use strict";

const assert = require("assert");
const path = require("path");
const { pathToFileURL } = require("url");

async function main() {
  const url = pathToFileURL(path.resolve(__dirname, "..", "src", "static", "scroll_jump.js")).href;
  const { computeVisibility } = await import(url);

  // Page not longer than viewport: both hidden.
  assert.deepStrictEqual(computeVisibility(0, 800, 800), { showTop: false, showBottom: false });
  assert.deepStrictEqual(computeVisibility(0, 700, 800), { showTop: false, showBottom: false });
  // Barely scrollable (within threshold): both hidden.
  assert.deepStrictEqual(computeVisibility(0, 900, 800), { showTop: false, showBottom: false });
  // At top of long page: only "down".
  assert.deepStrictEqual(computeVisibility(0, 5000, 800), { showTop: false, showBottom: true });
  assert.deepStrictEqual(computeVisibility(200, 5000, 800), { showTop: false, showBottom: true });
  // Middle: both.
  assert.deepStrictEqual(computeVisibility(1000, 5000, 800), { showTop: true, showBottom: true });
  // At bottom: only "up".
  assert.deepStrictEqual(computeVisibility(4200, 5000, 800), { showTop: true, showBottom: false });
  assert.deepStrictEqual(computeVisibility(4000, 5000, 800), { showTop: true, showBottom: false });
  // Custom threshold, negative/garbage scrollTop (overscroll).
  assert.deepStrictEqual(computeVisibility(50, 2000, 800, 20), { showTop: true, showBottom: true });
  assert.deepStrictEqual(computeVisibility(-30, 5000, 800), { showTop: false, showBottom: true });
  assert.deepStrictEqual(computeVisibility(0, NaN, 800), { showTop: false, showBottom: false });

  console.log("scroll_jump tests passed");
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
