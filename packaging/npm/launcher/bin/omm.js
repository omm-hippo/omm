#!/usr/bin/env node

"use strict";

const { LauncherError, ignoreBrokenPipe, run } = require("../lib/launcher.js");

ignoreBrokenPipe(process.stdout);
ignoreBrokenPipe(process.stderr);

function report(message) {
  try {
    process.stderr.write(`omm: ${message}\n`);
  } catch {
    // The launcher cannot report through a broken stderr; the exit code stands alone.
  }
}

try {
  process.exitCode = run(process.argv.slice(2));
} catch (error) {
  if (error instanceof LauncherError) {
    report(error.message);
  } else {
    report(`unexpected launcher failure: ${error && error.message ? error.message : String(error)}`);
  }
  process.exitCode = 1;
}
