import { useCallback, useEffect, useMemo, useState } from "react";
import api from "../api";

function membersFromDetail(detail) {
  if (!detail?.burst_group_id) return [];
  if (detail.burst_all_members?.length) return detail.burst_all_members;
  const self = {
    asset_id: detail.asset_id,
    stem: detail.stem,
    preview_path: detail.image_preview_path,
    is_keeper: detail.burst_is_keeper,
    matches_active_filter: true,
  };
  return [self, ...(detail.burst_siblings || [])];
}

export default function useBurstGallery({ detail, coverAssetId, personGroupId, onSelectAsset, pushToast, refreshGraph, t }) {
  const [expandedCoverId, setExpandedCoverId] = useState(null);
  const [burstFrameId, setBurstFrameId] = useState(null);
  const [burstSelectedIds, setBurstSelectedIds] = useState(() => new Set());
  const [burstMembers, setBurstMembers] = useState([]);

  const activeCoverId = expandedCoverId || null;
  const keeperId = useMemo(() => {
    const keeper = burstMembers.find((m) => m.is_keeper);
    return keeper?.asset_id || coverAssetId || detail?.asset_id || null;
  }, [burstMembers, coverAssetId, detail?.asset_id]);

  const loadBurstMembers = useCallback(async (assetId) => {
    if (!assetId) {
      setBurstMembers([]);
      return;
    }
    try {
      const payload = await api.getAssetDetailById(assetId, {
        personGroup: personGroupId || undefined,
      });
      setBurstMembers(membersFromDetail(payload));
    } catch {
      setBurstMembers([]);
    }
  }, [personGroupId]);

  useEffect(() => {
    if (!activeCoverId) {
      setBurstMembers([]);
      return;
    }
    void loadBurstMembers(activeCoverId);
  }, [activeCoverId, loadBurstMembers]);

  useEffect(() => {
    if (!activeCoverId) return;
    setBurstMembers(membersFromDetail(detail));
  }, [detail, activeCoverId]);

  const toggleExpand = useCallback((coverId) => {
    if (!coverId) return;
    setExpandedCoverId((current) => {
      if (current === coverId) {
        setBurstFrameId(null);
        setBurstSelectedIds(new Set());
        return null;
      }
      setBurstFrameId(null);
      setBurstSelectedIds(new Set());
      return coverId;
    });
  }, []);

  const selectBurstFrame = useCallback((assetId, event) => {
    if (!assetId) return;
    setBurstFrameId(assetId);
    if (event?.metaKey || event?.ctrlKey) {
      setBurstSelectedIds((prev) => {
        const next = new Set(prev);
        if (next.has(assetId)) next.delete(assetId);
        else next.add(assetId);
        return next;
      });
    } else if (event?.shiftKey && burstFrameId) {
      const ids = burstMembers.map((m) => m.asset_id);
      const a = ids.indexOf(burstFrameId);
      const b = ids.indexOf(assetId);
      if (a >= 0 && b >= 0) {
        const [lo, hi] = a < b ? [a, b] : [b, a];
        setBurstSelectedIds(new Set(ids.slice(lo, hi + 1)));
      }
    } else {
      setBurstSelectedIds(new Set([assetId]));
    }
    onSelectAsset?.(assetId);
  }, [burstFrameId, burstMembers, onSelectAsset]);

  const setBurstCover = useCallback(async (assetId, groupId) => {
    const gid = groupId || detail?.burst_group_id;
    if (!gid || !assetId) return;
    try {
      await api.setBurstKeeper({ groupId: gid, assetId });
      pushToast?.({ title: t?.("gallery.burstCoverUpdated") || "Cover updated", ttl: 2500 });
      await refreshGraph?.();
      if (activeCoverId) await loadBurstMembers(activeCoverId);
    } catch (err) {
      pushToast?.({
        title: t?.("gallery.burstCoverFailed") || "Could not set cover",
        message: err?.message || String(err),
        tone: "error",
        ttl: 5000,
      });
    }
  }, [activeCoverId, detail?.burst_group_id, loadBurstMembers, pushToast, refreshGraph, t]);

  const dragAssetIdsForBurst = useCallback((coverId, altKey) => {
    if (activeCoverId !== coverId || !burstMembers.length) return null;
    if (altKey) return burstMembers.map((m) => m.asset_id);
    const ids = burstSelectedIds.size
      ? [...burstSelectedIds]
      : burstFrameId
        ? [burstFrameId]
        : keeperId
          ? [keeperId]
          : [];
    return ids.filter(Boolean);
  }, [activeCoverId, burstFrameId, burstMembers, burstSelectedIds, keeperId]);

  return {
    expandedCoverId: activeCoverId,
    burstFrameId,
    burstSelectedIds,
    burstMembers,
    keeperId,
    toggleExpand,
    selectBurstFrame,
    setBurstCover,
    dragAssetIdsForBurst,
    collapseBurst: () => {
      setExpandedCoverId(null);
      setBurstFrameId(null);
      setBurstSelectedIds(new Set());
    },
  };
}
