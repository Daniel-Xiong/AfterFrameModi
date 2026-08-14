import { useMemo, useState } from "react";
import { Check, Copy, FolderInput, Link2, LoaderCircle, RefreshCw, ScanSearch, Trash2, X } from "lucide-react";
import { useTranslation } from "react-i18next";
import { formatBytes, localFileUrl } from "../utils/format";

const ATTACHABLE = new Set(["exact", "compressed_family", "crop_family"]);

function relationLabel(t, kind) {
  return t(`duplicates.kind.${kind}`, { defaultValue: kind.replaceAll("_", " ") });
}

function GroupCard({ group, onConfirm, onConfirmRaw, onDismiss, onDeleteCatalog, onTrash, onMove }) {
  const { t } = useTranslation("nav");
  const [keeperId, setKeeperId] = useState(group.representative_asset_id);
  const keeper = group.members?.find((member) => member.asset_id === keeperId);
  const removable = (group.members || []).filter((member) => member.asset_id !== keeperId);
  const reclaimable = removable.reduce((sum, member) => sum + Number(member.file_size || 0), 0);
  const rawMember = group.members?.find((member) => member.relation === "raw_candidate");

  return (
    <article className="rounded-2xl border border-border/60 bg-chrome/70 p-4 shadow-sm">
      <div className="mb-3 flex items-center justify-between gap-3">
        <div>
          <div className="text-[13px] font-semibold text-text">{relationLabel(t, group.kind)}</div>
          <div className="mt-0.5 text-[11px] text-muted2">
            {t("duplicates.members", { count: group.members?.length || 0 })}
            {reclaimable ? ` · ${t("duplicates.reclaim", { size: formatBytes(reclaimable) })}` : ""}
          </div>
        </div>
        <button className="rounded-lg p-2 text-muted2 hover:bg-hover hover:text-text" onClick={() => onDismiss(group.group_id)} title={t("duplicates.dismiss")}>
          <X className="h-4 w-4" />
        </button>
      </div>

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
        {(group.members || []).map((member) => {
          const selected = member.asset_id === keeperId;
          const evidence = member.evidence || {};
          return (
            <button
              key={member.asset_id}
              type="button"
              onClick={() => setKeeperId(member.asset_id)}
              className={`overflow-hidden rounded-xl border text-left transition ${selected ? "border-accent ring-1 ring-accent/50" : "border-border/50 hover:border-accent/40"}`}
            >
              <div className="relative aspect-[4/3] bg-hover">
                {member.preview_path ? (
                  <img src={localFileUrl(member.preview_path)} alt="" className="h-full w-full object-cover" />
                ) : (
                  <div className="flex h-full items-center justify-center text-muted2"><Copy className="h-6 w-6" /></div>
                )}
                {selected && (
                  <span className="absolute right-2 top-2 flex h-6 w-6 items-center justify-center rounded-full bg-accent text-app">
                    <Check className="h-4 w-4" />
                  </span>
                )}
              </div>
              <div className="space-y-1 p-2.5">
                <div className="truncate text-[12px] font-medium text-text">{member.stem}</div>
                <div className="truncate text-[10px] text-muted2">{member.canonical_path}</div>
                <div className="flex flex-wrap gap-1 text-[9px] text-muted">
                  <span>{member.relation}</span>
                  {formatBytes(member.file_size) && <span>· {formatBytes(member.file_size)}</span>}
                  {Number.isFinite(Number(evidence.hamming)) && <span>· Δ{evidence.hamming}</span>}
                  {evidence.crop_region_kind && <span>· {evidence.crop_region_kind}</span>}
                </div>
              </div>
            </button>
          );
        })}
      </div>

      <div className="mt-4 flex flex-wrap items-center gap-2">
        {ATTACHABLE.has(group.kind) && (
          <button className="inline-flex items-center gap-1.5 rounded-lg bg-accent px-3 py-2 text-[11px] font-semibold text-app" onClick={() => onConfirm(group.group_id, keeperId)}>
            <Link2 className="h-3.5 w-3.5" />
            {t("duplicates.attach")}
          </button>
        )}
        {group.kind === "raw_proposal" && rawMember && (
          <button className="inline-flex items-center gap-1.5 rounded-lg bg-accent px-3 py-2 text-[11px] font-semibold text-app" onClick={() => onConfirmRaw(group.group_id, rawMember.asset_id)}>
            <Link2 className="h-3.5 w-3.5" />
            {t("duplicates.confirmRaw")}
          </button>
        )}
        {!!removable.length && (
          <>
            <button className="rounded-lg border border-border px-3 py-2 text-[11px] text-text hover:bg-hover" onClick={() => onDeleteCatalog(removable)}>
              {t("duplicates.removeCatalog")}
            </button>
            <button className="inline-flex items-center gap-1.5 rounded-lg border border-danger/40 px-3 py-2 text-[11px] text-danger hover:bg-danger/10" onClick={() => onTrash(removable)}>
              <Trash2 className="h-3.5 w-3.5" />
              {t("duplicates.trash")}
            </button>
            <button className="inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-2 text-[11px] text-text hover:bg-hover" onClick={() => onMove(removable)}>
              <FolderInput className="h-3.5 w-3.5" />
              {t("duplicates.move")}
            </button>
          </>
        )}
        <span className="ml-auto text-[10px] text-muted2">
          {t("duplicates.keeper", { name: keeper?.stem || "" })}
        </span>
      </div>
    </article>
  );
}

export default function DuplicatesView({ similarity, roots, onDeleteCatalog, onTrash, onMove }) {
  const { t } = useTranslation("nav");
  const imageRoots = useMemo(
    () => (roots || []).filter((root) => root.root_type === "image" && root.user_declared !== false),
    [roots],
  );
  const [probeRootId, setProbeRootId] = useState(imageRoots[0]?.root_id || "");
  const [scope, setScope] = useState("catalog_except_probe");

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden">
      <header className="flex flex-wrap items-center gap-3 border-b border-border/50 bg-chrome px-5 py-3">
        <div className="mr-auto">
          <h1 className="text-[15px] font-semibold text-text">{t("duplicates.title")}</h1>
          <p className="text-[11px] text-muted2">{t("duplicates.subtitle")}</p>
        </div>
        <select value={probeRootId} onChange={(event) => setProbeRootId(event.target.value)} className="max-w-[280px] rounded-lg border border-border bg-app px-3 py-2 text-[11px] text-text">
          <option value="">{t("duplicates.chooseRoot")}</option>
          {imageRoots.map((root) => <option key={root.root_id} value={root.root_id}>{root.path}</option>)}
        </select>
        <select value={scope} onChange={(event) => setScope(event.target.value)} className="rounded-lg border border-border bg-app px-3 py-2 text-[11px] text-text">
          <option value="catalog_except_probe">{t("duplicates.againstCatalog")}</option>
          <option value="same_root">{t("duplicates.insideRoot")}</option>
        </select>
        <button disabled={!probeRootId || similarity.scan.active} onClick={() => similarity.start({ probeRootId, galleryScope: scope })} className="inline-flex items-center gap-1.5 rounded-lg bg-accent px-3 py-2 text-[11px] font-semibold text-app disabled:opacity-40">
          {similarity.scan.active ? <LoaderCircle className="h-3.5 w-3.5 animate-spin" /> : <ScanSearch className="h-3.5 w-3.5" />}
          {similarity.scan.active ? t("duplicates.scanning", { percent: Math.round(similarity.scan.progress * 100) }) : t("duplicates.scan")}
        </button>
        <button onClick={similarity.load} className="rounded-lg p-2 text-muted2 hover:bg-hover hover:text-text" title={t("duplicates.refresh")}>
          <RefreshCw className="h-4 w-4" />
        </button>
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto p-5">
        {similarity.loading ? (
          <div className="flex h-full items-center justify-center text-muted2"><LoaderCircle className="h-5 w-5 animate-spin" /></div>
        ) : similarity.failed ? (
          <div className="flex h-full items-center justify-center text-sm text-danger">{t("duplicates.failed")}</div>
        ) : !similarity.groups.length ? (
          <div className="flex h-full flex-col items-center justify-center text-center">
            <ScanSearch className="mb-3 h-10 w-10 text-muted2" />
            <div className="text-[14px] font-medium text-text">{t("duplicates.emptyTitle")}</div>
            <div className="mt-1 max-w-md text-[11px] text-muted2">{t("duplicates.emptyHint")}</div>
          </div>
        ) : (
          <div className="mx-auto max-w-6xl space-y-4">
            {similarity.groups.map((group) => (
              <GroupCard
                key={group.group_id}
                group={group}
                onConfirm={similarity.confirm}
                onConfirmRaw={similarity.confirmRaw}
                onDismiss={similarity.dismiss}
                onDeleteCatalog={onDeleteCatalog}
                onTrash={onTrash}
                onMove={onMove}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
