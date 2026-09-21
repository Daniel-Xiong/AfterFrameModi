import { useMemo } from "react";
import { useTranslation } from "react-i18next";
import { X, Sparkles } from "lucide-react";
import { localFileUrl } from "../utils/format";

function clusterKey(cluster) {
  return cluster?.cluster_id || (cluster?.asset_ids || []).join(",");
}

export default function SimilarSessionBanner({
  clusters = [],
  itemsById,
  keeperIds,
  collectionMemberIds,
  checkedByCluster,
  onToggleChecked,
  onClose,
  onRemoveChecked,
  onKeepAll,
  busy = false,
}) {
  const { t } = useTranslation("nav");
  const totalChecked = useMemo(() => {
    let n = 0;
    for (const cluster of clusters) {
      const key = clusterKey(cluster);
      const set = checkedByCluster?.[key];
      if (set) n += set.size;
    }
    return n;
  }, [clusters, checkedByCluster]);

  if (!clusters?.length) return null;

  return (
    <div
      className="shrink-0 border-b border-[rgba(239,200,80,0.35)] bg-[rgba(120,90,8,0.22)] px-3 py-2"
      data-testid="similar-session-banner"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2 text-[12px] text-[#FAEEDA]">
          <Sparkles className="h-3.5 w-3.5 shrink-0" />
          <span className="font-medium">{t("similar.sessionTitle", { count: clusters.length })}</span>
          <span className="text-[#c9b896]">{t("similar.sessionHint")}</span>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            disabled={busy || totalChecked < 1}
            className="rounded-md bg-[#EF9F27] px-2.5 py-1 text-[11px] font-medium text-[#412402] disabled:opacity-50"
            onClick={() => onRemoveChecked?.()}
          >
            {t("similar.removeChecked", { count: totalChecked })}
          </button>
          <button
            type="button"
            className="rounded-md px-2 py-1 text-[11px] text-[#FAEEDA] hover:bg-black/20"
            onClick={() => onKeepAll?.()}
          >
            {t("similar.keepAll")}
          </button>
          <button
            type="button"
            className="flex h-7 w-7 items-center justify-center rounded-md text-[#FAEEDA] hover:bg-black/20"
            title={t("similar.close")}
            onClick={() => onClose?.()}
          >
            <X className="h-4 w-4" />
          </button>
        </div>
      </div>
      <div className="mt-2 max-h-[220px] space-y-2 overflow-y-auto pr-1">
        {clusters.map((cluster) => {
          const key = clusterKey(cluster);
          const checked = checkedByCluster?.[key] || new Set();
          return (
            <div key={key} className="flex flex-wrap items-start gap-2 rounded-lg bg-black/20 px-2 py-1.5">
              {(cluster.asset_ids || []).map((assetId, index) => {
                const item = itemsById?.get(assetId);
                const stem = cluster.stems?.[index] || item?.stem || assetId;
                const isKeeper = keeperIds?.has(assetId);
                const inCollection = collectionMemberIds?.has(assetId);
                const suggested = !isKeeper && !inCollection && assetId !== cluster.asset_ids?.[0];
                const isChecked = checked.has(assetId);
                return (
                  <label
                    key={assetId}
                    className={[
                      "relative flex w-20 flex-col gap-1 rounded-md p-1",
                      suggested ? "bg-black/25" : "opacity-80",
                    ].join(" ")}
                  >
                    <input
                      type="checkbox"
                      className="absolute left-1 top-1 z-10 h-3 w-3"
                      checked={isChecked}
                      onChange={() => onToggleChecked?.(key, assetId)}
                    />
                    <div className="h-14 w-full overflow-hidden rounded bg-black">
                      {item?.preview_path || item?.image_path ? (
                        <img
                          src={localFileUrl(item.preview_path || item.image_path)}
                          alt={stem}
                          className="h-full w-full object-cover"
                          draggable={false}
                        />
                      ) : null}
                    </div>
                    <span className="truncate text-[9px] text-[#e8dcc4]">{stem}</span>
                  </label>
                );
              })}
            </div>
          );
        })}
      </div>
    </div>
  );
}
