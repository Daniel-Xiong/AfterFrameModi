const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const { fullHash } = require("./relocation");

test("fullHash detects copied content and corruption", async () => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "afterframe-relocate-"));
  try {
    const source = path.join(root, "source.bin");
    const copy = path.join(root, "copy.bin");
    await fs.writeFile(source, Buffer.from("verified-content"));
    await fs.copyFile(source, copy);
    assert.equal(await fullHash(source), await fullHash(copy));
    await fs.appendFile(copy, Buffer.from("-corrupt"));
    assert.notEqual(await fullHash(source), await fullHash(copy));
  } finally {
    await fs.rm(root, { recursive: true, force: true });
  }
});
