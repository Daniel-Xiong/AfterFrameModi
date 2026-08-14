// Asset-level IPC: quick-register (after a save), collage-sources lookup,
// delete-image-assets, and the cross-platform "reveal in Finder/Explorer".

function register({ ipcMain, shell, dialog, BrowserWindow, commands, callSidecarJsonAsync, addAllowedMediaDir, getCatalogState, t }) {
  const fs = require("node:fs");
  const path = require("node:path");
  // t() returns a translator bound to the current locale; fall back to identity
  // (English literals) if i18n wasn't wired (e.g. older callers/tests).
  const tr = () => (typeof t === "function" ? t() : (key) => key);

  ipcMain.handle("workspace:reveal", (_event, targetPath) => {
    if (!targetPath) return false;
    // Don't silently no-op on a moved/deleted original — report it so the
    // renderer can toast instead of opening Finder with nothing selected.
    if (!fs.existsSync(targetPath)) return false;
    shell.showItemInFolder(targetPath);
    return true;
  });

  // Full-library missing-file sweep (File ▸ Verify Files menu action).
  ipcMain.handle("workspace:verify-assets", async (_event, options) => {
    const { currentCatalogPath, catalogHasDb } = getCatalogState?.() || {};
    if (!currentCatalogPath || !catalogHasDb?.()) return null;
    return await commands.verifyAssets(options || {});
  });

  // Relink a missing asset to a new on-disk location. With no newPath we open a
  // native file picker. Returns the sidecar result verbatim, including the
  // {status:"fingerprint_mismatch"} case so the renderer can confirm + retry.
  ipcMain.handle("workspace:relink-asset", async (_event, options) => {
    const opts = options || {};
    if (!opts.assetId) return null;
    let newPath = opts.newPath;
    if (!newPath) {
      const parent = BrowserWindow.getFocusedWindow();
      const translate = tr();
      const result = await dialog.showOpenDialog(parent, {
        title: translate("dialog.relinkTitle"),
        properties: ["openFile"],
        message: translate("dialog.relinkMessage"),
      });
      if (result.canceled || !result.filePaths?.length) return { status: "cancelled" };
      newPath = result.filePaths[0];
    }
    const outcome = await commands.relinkAsset({ assetId: opts.assetId, newPath, force: !!opts.force });
    // Let media:// load the relinked original right away.
    if (outcome?.status === "relinked" && outcome.new_path) {
      addAllowedMediaDir?.(path.dirname(outcome.new_path));
    }
    return outcome;
  });

  ipcMain.handle("workspace:open-external", (_event, url) => {
    if (!url || typeof url !== "string") return false;
    // Only allow http(s) so a misuse can't, e.g., launch file:// or javascript:.
    if (!/^https?:\/\//i.test(url)) return false;
    shell.openExternal(url);
    return true;
  });

  ipcMain.handle("workspace:quick-register", async (_event, imagePath, originPath, collageSourceIds) => {
    if (!imagePath) return null;
    // The renderer will media:// this file right after registering it.
    addAllowedMediaDir?.(require("node:path").dirname(imagePath));
    return await commands.quickRegister({ imagePath, originPath, collageSourceIds });
  });

  ipcMain.handle("workspace:collage-sources", async (_event, assetId) => {
    if (!assetId) return { sources: [], used_in_collages: [] };
    return await callSidecarJsonAsync(["collage-sources", "--asset-id", assetId]);
  });

  // Cheap watched-dir catch-up check (startup): returns only media files not yet
  // in the catalog (and not user-deleted). No EXIF/matching — the renderer then
  // imports just the new files, so an unchanged library shows no import at all.
  ipcMain.handle("workspace:scan-new-media", async (_event, dirs) => {
    const ds = [...new Set((dirs || []).filter(Boolean))];
    if (!ds.length) return { new_files: [], scanned: 0 };
    const command = ["scan-new-media"];
    for (const d of ds) command.push("--image-dir", String(d));
    return await callSidecarJsonAsync(command) || { new_files: [], scanned: 0 };
  });

  ipcMain.handle("workspace:delete-image-assets", async (_event, assetIds) => {
    const ids = [...new Set((assetIds || []).filter(Boolean))];
    if (!ids.length) return [];
    const command = ["delete-image-assets"];
    for (const assetId of ids) command.push("--asset-id", String(assetId));
    return await callSidecarJsonAsync(command) || [];
  });

  // Delete from disk: resolve authoritative paths from the catalog, trash each
  // file, then remove ONLY the successfully trashed subset from the catalog.
  // A partial OS failure must never make a still-existing file disappear from
  // the catalog.
  ipcMain.handle("workspace:delete-image-assets-from-disk", async (_event, payload) => {
    const requested = Array.isArray(payload?.items)
      ? payload.items
      : (payload?.assetIds || []).map((assetId, index) => ({
          assetId,
          path: payload?.paths?.[index],
        }));
    const ids = [...new Set(requested.map((item) => item?.assetId).filter(Boolean))];
    if (!ids.length) return { deleted: [], trashed: 0, failed: [] };
    const failed = [];
    const trashedIds = [];
    let trashed = 0;
    for (const assetId of ids) {
      try {
        const detail = await commands.assetDetail({ assetId });
        const authoritativePath = detail?.image_path || detail?.canonical_path;
        if (!authoritativePath) {
          failed.push({ assetId, error: "catalog_path_missing" });
          continue;
        }
        const requestedPath = requested.find((item) => item?.assetId === assetId)?.path;
        if (requestedPath && path.resolve(requestedPath) !== path.resolve(authoritativePath)) {
          failed.push({ assetId, path: requestedPath, error: "catalog_path_mismatch" });
          continue;
        }
        if (!fs.existsSync(authoritativePath)) {
          failed.push({ assetId, path: authoritativePath, error: "file_missing" });
          continue;
        }
        await shell.trashItem(authoritativePath);
        trashed += 1;
        trashedIds.push(assetId);
      } catch (err) {
        failed.push({ assetId, error: err?.message || String(err) });
      }
    }
    if (!trashedIds.length) return { deleted: [], trashed, failed };
    const command = ["delete-image-assets"];
    for (const assetId of trashedIds) command.push("--asset-id", String(assetId));
    const deleted = await callSidecarJsonAsync(command) || [];
    return { deleted, trashed, failed };
  });
}

module.exports = { register };
