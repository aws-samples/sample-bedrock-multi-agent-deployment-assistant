#!/usr/bin/env node
/**
 * Mermaid diagram validator — reads diagram code from stdin,
 * validates it using mermaid.parse(), and outputs JSON to stdout.
 *
 * Output format:
 *   { "valid": true }
 *   { "valid": false, "error": "Parse error at line 3: ..." }
 *
 * Usage:
 *   echo "architecture-beta\n  service r_A(aws:ec2)[Test]" | node index.mjs
 */

import mermaid from "mermaid";

mermaid.initialize({
  startOnLoad: false,
  // Suppress log output — we only want JSON on stdout
  logLevel: "error",
  securityLevel: "strict",
});

function readStdin() {
  return new Promise((resolve, reject) => {
    const chunks = [];
    process.stdin.setEncoding("utf-8");
    process.stdin.on("data", (chunk) => chunks.push(chunk));
    process.stdin.on("end", () => resolve(chunks.join("")));
    process.stdin.on("error", reject);
  });
}

async function main() {
  try {
    const code = await readStdin();

    if (!code.trim()) {
      console.log(JSON.stringify({ valid: false, error: "Empty diagram code" }));
      process.exit(0);
    }

    // mermaid.parse() returns true or throws on invalid syntax
    await mermaid.parse(code.trim());
    console.log(JSON.stringify({ valid: true }));
  } catch (err) {
    const message =
      err instanceof Error ? err.message : String(err);
    console.log(JSON.stringify({ valid: false, error: message }));
  }
}

main();
