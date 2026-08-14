const fs = require("node:fs");
const fsp = require("node:fs/promises");
const path = require("node:path");
const crypto = require("node:crypto");

async function fullHash(filePath) {
  const hash = crypto.createHash("sha256");
  await new Promise((resolve, reject) => {
    const stream = fs.createReadStream(filePath);
    stream.on("data", (chunk) => hash.update(chunk));
    stream.on("error", reject);
    stream.on("end", resolve);
  });
  return hash.digest("hex");
}

async function availableBytes(directory) {
  if (typeof fsp.statfs !== "function") return null;
  const stats = await fsp.statfs(directory, { bigint: true });
  return stats.bavail * stats.bsize;
}

function register({ ipcMain, dialog, BrowserWindow, shell, commands, watcherApi, addAllowedMediaDir }) {
  ipcMain.handle("workspace:list-relocations", async (_event, options) => {
    return await commands.listRelocations(options || {});
  });

  ipcMain.handle("workspace:relocate-assets", async (_event, payload) => {
    const items = Array.isArray(payload?.items) ? payload.items : [];
    const mode = payload?.mode === "archive" ? "archive" : "move";
    if (!items.length) return { completed: [], failed: [] };
    let destinationDir = payload?.destinationDir;
    if (!destinationDir) {
      const result = await dialog.showOpenDialog(BrowserWindow.getFocusedWindow(), {
        title: mode === "archive" ? "Choose archive folder" : "Choose destination folder",
        properties: ["openDirectory", "createDirectory"],
      });
      if (result.canceled || !result.filePaths?.length) {
        return { cancelled: true, completed: [], failed: [] };
      }
      [destinationDir] = result.filePaths;
    }
    destinationDir = path.resolve(destinationDir);
    await fsp.access(destinationDir, fs.constants.W_OK);
    addAllowedMediaDir?.(destinationDir);

    const completed = [];
    const failed = [];
    for (const requested of items) {
      const assetId = requested?.assetId;
      let operation = null;
      let source = null;
      let destination = null;
      let partial = null;
      let sameVolume = false;
      let destinationCreated = false;
      let catalogRelinked = false;
      try {
        const detail = await commands.assetDetail({ assetId });
        const catalogPath = detail?.image_path || detail?.canonical_path;
        if (!catalogPath) throw new Error("catalog_path_missing");
        source = path.resolve(catalogPath);
        const sourceStat = await fsp.lstat(source);
        if (sourceStat.isSymbolicLink()) throw new Error("symbolic_links_not_supported");
        if (!sourceStat.isFile()) throw new Error("source_not_regular_file");
        if (sourceStat.nlink > 1) throw new Error("hard_links_require_manual_move");
        destination = path.join(destinationDir, path.basename(source));
        partial = `${destination}.partial-${crypto.randomBytes(5).toString("hex")}`;
        try {
          await fsp.lstat(destination);
          throw new Error("destination_exists");
        } catch (error) {
          if (error.code !== "ENOENT") throw error;
        }
        const destinationStat = await fsp.stat(destinationDir);
        sameVolume = sourceStat.dev === destinationStat.dev;
        if (!sameVolume) {
          const free = await availableBytes(destinationDir);
          if (free != null && free < BigInt(sourceStat.size)) throw new Error("insufficient_destination_space");
        }
        const expectedHash = await fullHash(source);
        operation = await commands.createRelocation({
          assetId,
          sourcePath: source,
          destinationPath: destination,
          mode,
          expectedSize: sourceStat.size,
          expectedHash,
        });
        watcherApi?.suppress?.([source, destination, partial]);
        if (sameVolume) {
          await fsp.rename(source, destination);
          destinationCreated = true;
          await commands.updateRelocation({ operationId: operation.operation_id, state: "renamed" });
        } else {
          await fsp.copyFile(source, partial, fs.constants.COPYFILE_EXCL);
          await commands.updateRelocation({ operationId: operation.operation_id, state: "copied" });
          const copiedStat = await fsp.stat(partial);
          if (copiedStat.size !== sourceStat.size || await fullHash(partial) !== expectedHash) {
            throw new Error("copy_verification_failed");
          }
          await fsp.rename(partial, destination);
          destinationCreated = true;
          await commands.updateRelocation({ operationId: operation.operation_id, state: "renamed" });
        }
        if (await fullHash(destination) !== expectedHash) throw new Error("destination_verification_failed");
        await commands.updateRelocation({ operationId: operation.operation_id, state: "verified" });

        if (mode === "move") {
          const relinked = await commands.relinkAsset({ assetId, newPath: destination });
          if (relinked?.status !== "relinked") throw new Error(`relink_failed:${relinked?.status || "unknown"}`);
          catalogRelinked = true;
          await commands.updateRelocation({ operationId: operation.operation_id, state: "relinked" });
          if (!sameVolume) {
            await shell.trashItem(source);
            await commands.updateRelocation({ operationId: operation.operation_id, state: "source_trashed" });
          }
        } else {
          if (!sameVolume) await shell.trashItem(source);
          await commands.deleteImageAssets([assetId]);
          await commands.updateRelocation({ operationId: operation.operation_id, state: "source_trashed" });
        }
        await commands.updateRelocation({ operationId: operation.operation_id, state: "complete" });
        completed.push({ assetId, destination, operationId: operation.operation_id });
      } catch (error) {
        let recoveryNeeded = false;
        try {
          if (partial) await fsp.rm(partial, { force: true });
          if (catalogRelinked) {
            recoveryNeeded = true;
          } else if (destinationCreated && source) {
            if (sameVolume && !fs.existsSync(source)) {
              await fsp.rename(destination, source);
            } else if (!sameVolume && fs.existsSync(source)) {
              await fsp.rm(destination, { force: true });
            } else {
              recoveryNeeded = true;
            }
          }
        } catch {
          recoveryNeeded = true;
        }
        if (operation?.operation_id) {
          try {
            await commands.updateRelocation({
              operationId: operation.operation_id,
              state: recoveryNeeded ? "recovery_needed" : "failed",
              errorText: error?.message || String(error),
            });
          } catch {}
        }
        failed.push({
          assetId,
          source,
          destination,
          operationId: operation?.operation_id || null,
          error: error?.message || String(error),
          recoveryNeeded,
        });
      } finally {
        watcherApi?.release?.([source, destination, partial].filter(Boolean));
      }
    }
    return { completed, failed, cancelled: false };
  });
}

module.exports = { register, fullHash };
