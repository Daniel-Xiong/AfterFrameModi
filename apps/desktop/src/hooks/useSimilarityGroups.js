import { useCallback, useEffect, useRef, useState } from "react";
import api from "../api";

export default function useSimilarityGroups({ enabled, catalogKey, pushToast }) {
  const [groups, setGroups] = useState([]);
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);
  const [scan, setScan] = useState({ active: false, progress: 0, status: null });
  const wasActive = useRef(false);

  const load = useCallback(async () => {
    setLoading(true);
    setFailed(false);
    try {
      const rows = await api.listSimilarityGroups({ status: "pending", limit: 500 });
      setGroups(Array.isArray(rows) ? rows : []);
    } catch (error) {
      console.warn("[useSimilarityGroups] list failed", error);
      setFailed(true);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    setGroups([]);
    setFailed(false);
    setScan({ active: false, progress: 0, status: null });
    wasActive.current = false;
  }, [catalogKey]);

  useEffect(() => {
    if (enabled) void load();
  }, [enabled, load]);

  useEffect(() => {
    if (!enabled) return undefined;
    let cancelled = false;
    async function poll() {
      try {
        const status = await api.similarityStatus();
        if (cancelled || !status) return;
        const active = !!status.active;
        setScan({
          active,
          progress: Number(status.progress) || 0,
          status: status.status || null,
          error: status.error || null,
        });
        if (wasActive.current && !active) {
          if (status.status === "failed") {
            pushToast?.({ title: "Similarity scan failed", message: status.error || "", tone: "error", ttl: 7000 });
          }
          void load();
        }
        wasActive.current = active;
      } catch {
        // Advisory status; the persisted group list remains usable.
      }
    }
    void poll();
    const timer = setInterval(poll, 2000);
    return () => { cancelled = true; clearInterval(timer); };
  }, [enabled, load, pushToast]);

  const start = useCallback(async ({ probeRootId, galleryScope = "catalog_except_probe" }) => {
    const status = await api.startSimilarityScan({
      probeRootId,
      galleryScope,
      includeRawProposals: true,
    });
    setScan({ active: !!status?.active, progress: Number(status?.progress) || 0, status: status?.status || null });
    wasActive.current = !!status?.active;
    return status;
  }, []);

  const dismiss = useCallback(async (groupId) => {
    await api.dismissSimilarityGroup(groupId);
    setGroups((current) => current.filter((group) => group.group_id !== groupId));
  }, []);

  const confirm = useCallback(async (groupId, keeperAssetId) => {
    const result = await api.confirmSimilarityGroup({ groupId, keeperAssetId });
    setGroups((current) => current.filter((group) => group.group_id !== groupId));
    return result;
  }, []);

  const confirmRaw = useCallback(async (groupId, rawAssetId) => {
    const result = await api.confirmRawSimilarity({ groupId, rawAssetId });
    setGroups((current) => current.filter((group) => group.group_id !== groupId));
    return result;
  }, []);

  const removeGroupsContaining = useCallback((assetIds) => {
    const removed = new Set(assetIds || []);
    setGroups((current) => current.filter(
      (group) => !group.members?.some((member) => removed.has(member.asset_id)),
    ));
  }, []);

  const relocate = useCallback(async (members, { mode = "move" } = {}) => {
    const result = await api.relocateAssets({
      items: (members || []).map((member) => ({ assetId: member.asset_id })),
      mode,
    });
    if (result?.failed?.length) {
      pushToast?.({
        title: "Some files could not be moved",
        message: `${result.failed.length} failed`,
        tone: "error",
        ttl: 6000,
      });
    }
    await load();
    return result;
  }, [load, pushToast]);

  return {
    groups,
    loading,
    failed,
    scan,
    load,
    start,
    dismiss,
    confirm,
    confirmRaw,
    relocate,
    removeGroupsContaining,
  };
}
