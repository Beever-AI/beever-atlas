#!/usr/bin/env node
// Builds beever-atlas-bot.zip from manifest.json + generated placeholder icons.
// Replace outline.png / color.png with real artwork before production release.

import { writeFileSync } from "node:fs";
import { deflateSync } from "node:zlib";
import { execSync } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));

const CRC_TABLE = (() => {
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    t[n] = c;
  }
  return t;
})();

function crc32(buf) {
  let c = 0xffffffff;
  for (const b of buf) c = CRC_TABLE[(c ^ b) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

function chunk(type, data) {
  const len = Buffer.alloc(4);
  len.writeUInt32BE(data.length, 0);
  const body = Buffer.concat([Buffer.from(type, "ascii"), data]);
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(body), 0);
  return Buffer.concat([len, body, crc]);
}

function solidPng(w, h, r, g, b, a = 255) {
  const sig = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(w, 0);
  ihdr.writeUInt32BE(h, 4);
  ihdr[8] = 8;
  ihdr[9] = 6;
  const rowLen = 1 + w * 4;
  const raw = Buffer.alloc(rowLen * h);
  for (let y = 0; y < h; y++) {
    const off = y * rowLen;
    for (let x = 0; x < w; x++) {
      const p = off + 1 + x * 4;
      raw[p] = r;
      raw[p + 1] = g;
      raw[p + 2] = b;
      raw[p + 3] = a;
    }
  }
  return Buffer.concat([
    sig,
    chunk("IHDR", ihdr),
    chunk("IDAT", deflateSync(raw)),
    chunk("IEND", Buffer.alloc(0)),
  ]);
}

writeFileSync(join(here, "color.png"), solidPng(192, 192, 0x5b, 0x4e, 0xcc));
writeFileSync(join(here, "outline.png"), solidPng(32, 32, 0xff, 0xff, 0xff));
console.log("✓ generated color.png (192x192) and outline.png (32x32)");

execSync(
  `cd "${here}" && rm -f beever-atlas-bot.zip && zip -j beever-atlas-bot.zip manifest.json color.png outline.png`,
  { stdio: "inherit" },
);
console.log(`\n✓ built ${join(here, "beever-atlas-bot.zip")}`);
console.log(
  "\nSideload: Teams → Apps → Manage your apps → Upload a custom app → pick this zip",
);
