#!/usr/bin/env node

"use strict";

const { run } = require("../lib/launcher.js");

process.exitCode = run(process.argv.slice(2));
