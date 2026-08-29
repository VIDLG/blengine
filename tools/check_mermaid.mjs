import { readdir, readFile, stat } from "node:fs/promises";
import path from "node:path";
import process from "node:process";

import { JSDOM } from "jsdom";

const dom = new JSDOM();
globalThis.window = dom.window;
globalThis.document = dom.window.document;

const { default: mermaid } = await import("mermaid");

const DEFAULT_PATHS = [".plan/PLAN.md", "docs/research", "docs/testing"];

async function markdownFiles(inputPath) {
  const entry = await stat(inputPath);
  if (entry.isFile()) {
    return inputPath.endsWith(".md") ? [inputPath] : [];
  }

  const entries = await readdir(inputPath, { withFileTypes: true });
  const nested = await Promise.all(
    entries.map((child) => markdownFiles(path.join(inputPath, child.name))),
  );
  return nested.flat();
}

function mermaidBlocks(markdown) {
  const lines = markdown.split(/\r?\n/);
  const blocks = [];
  let startLine;
  let body = [];

  for (const [index, line] of lines.entries()) {
    if (startLine === undefined) {
      if (/^\s*```mermaid\s*$/.test(line)) {
        startLine = index + 2;
        body = [];
      }
      continue;
    }

    if (/^\s*```\s*$/.test(line)) {
      blocks.push({ line: startLine, source: body.join("\n") });
      startLine = undefined;
      continue;
    }

    body.push(line);
  }

  if (startLine !== undefined) {
    throw new Error(`unclosed Mermaid fence starting at line ${startLine - 1}`);
  }

  return blocks;
}

// Mermaid's strict sanitizer expects a browser DOM even for parse-only flowcharts.
// HTML-like labels are rejected below, so loose mode is safe for this syntax check.
mermaid.initialize({ startOnLoad: false, securityLevel: "loose" });

const inputs = process.argv.slice(2);
const files = (
  await Promise.all((inputs.length ? inputs : DEFAULT_PATHS).map(markdownFiles))
)
  .flat()
  .sort();

let checked = 0;
let failed = false;

for (const file of files) {
  let blocks;
  try {
    blocks = mermaidBlocks(await readFile(file, "utf8"));
  } catch (error) {
    console.error(`${file}: ${error.message}`);
    failed = true;
    continue;
  }

  for (const block of blocks) {
    checked += 1;
    if (/<[A-Za-z/(]/.test(block.source)) {
      console.error(
        `${file}:${block.line}: HTML-like angle bracket in Mermaid text; use plain words instead`,
      );
      failed = true;
      continue;
    }

    try {
      await mermaid.parse(block.source, { suppressErrors: false });
    } catch (error) {
      console.error(`${file}:${block.line}: ${error.message}`);
      failed = true;
    }
  }
}

if (failed) {
  process.exitCode = 1;
} else {
  console.log(`Checked ${checked} Mermaid diagrams in ${files.length} Markdown files`);
}
