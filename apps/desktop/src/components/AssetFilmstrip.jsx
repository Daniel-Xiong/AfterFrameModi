import { useTranslation } from "react-i18next";
import { localFileUrl } from "../utils/format";

/**
 * Horizontal thumbnail strip for burst members or version siblings.
 */
export default function AssetFilmstrip({
  items = [],
  selectedId,
  selectedIds,
  keeperId,
  onSelect,
  onContextMenu,
  dimUnmatched = false,
  showKeeperBadge = false,
  className = "",
}) {
  const { t } = useTranslation("nav");
  const multi = selectedIds instanceof Set ? selectedIds : null;

  if (!items?.length) return null;

  return (
    <div
      className={[
        "flex gap-1.5 overflow-x-auto pb-1 pt-0.5",
        className,
      ].join(" ")}
      data-asset-filmstrip="true"
      onClick={(event) => event.stopPropagation()}
    >
      {items.map((item) => {
        const id = item.asset_id;
        const selected = multi ? multi.has(id) : id === selectedId;
        const dimmed = dimUnmatched && item.matches_active_filter === false;
        const isKeeper = keeperId && id === keeperId;
        return (
          <button
            key={id}
            type="button"
            title={item.stem}
            className={[
              "relative h-16 w-16 shrink-0 overflow-hidden rounded-md bg-black transition-all",
              selected ? "ring-2 ring-accent" : "ring-1 ring-border/50 hover:ring-accent/45",
              dimmed ? "opacity-45" : "",
            ].join(" ")}
            onClick={(event) => onSelect?.(id, event)}
            onContextMenu={(event) => onContextMenu?.(event, item)}
          >
            {item.preview_path ? (
              <img
                src={localFileUrl(item.preview_path)}
                alt={item.stem || ""}
                className="h-full w-full object-cover"
                loading="lazy"
                draggable={false}
              />
            ) : (
              <div className="flex h-full w-full items-center justify-center text-[9px] text-muted2">—</div>
            )}
            {showKeeperBadge && isKeeper ? (
              <span className="absolute bottom-0.5 left-0.5 rounded bg-black/75 px-1 py-px text-[8px] font-medium text-accent">
                {t("gallery.burstCover")}
              </span>
            ) : null}
          </button>
        );
      })}
    </div>
  );
}
