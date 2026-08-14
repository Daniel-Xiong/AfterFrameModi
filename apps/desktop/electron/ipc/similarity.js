function register({ ipcMain, commands, startVisualMatchTask, getCatalogState, formatJobStatus, latestJobStatus }) {
  function hasCatalog() {
    const { currentCatalogPath, catalogHasDb } = getCatalogState();
    return !!currentCatalogPath && !!catalogHasDb();
  }

  ipcMain.handle("workspace:similarity-status", async () => {
    if (!hasCatalog()) return formatJobStatus(null);
    try {
      return await latestJobStatus("visual_match");
    } catch {
      return formatJobStatus(null);
    }
  });

  ipcMain.handle("workspace:similarity-start", async (_event, options) => {
    if (!hasCatalog()) return formatJobStatus(null);
    return await startVisualMatchTask(options || {});
  });

  ipcMain.handle("workspace:similarity-groups", async (_event, options) => {
    if (!hasCatalog()) return [];
    return await commands.listSimilarityGroups(options || {});
  });

  ipcMain.handle("workspace:similarity-dismiss", async (_event, groupId) => {
    if (!hasCatalog() || !groupId) return null;
    return await commands.dismissSimilarityGroup(groupId);
  });

  ipcMain.handle("workspace:similarity-confirm", async (_event, options) => {
    if (!hasCatalog() || !options?.groupId) return null;
    return await commands.confirmSimilarityGroup(options);
  });

  ipcMain.handle("workspace:similarity-confirm-raw", async (_event, options) => {
    if (!hasCatalog() || !options?.groupId) return null;
    return await commands.confirmRawSimilarity(options);
  });
}

module.exports = { register };
