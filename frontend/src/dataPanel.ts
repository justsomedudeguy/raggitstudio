export type BrowserOpener = (url: string, target: string, features: string) => Window | null | void;
export type RedditArchiveTargetType = "subreddit" | "user";
export interface RedditArchiveImportPlanItem {
  kind: "post" | "comment";
  path: string;
}

export interface ArchivePipelineSummary {
  items: number;
  metadata_items?: number | null;
  classifier_items?: number | null;
  classifier_skipped_items?: number | null;
  semantic_chunks: number;
  embedded_items: number;
  semantic_index_state?: string | null;
  resumable_import?: boolean | null;
  active_import_status?: string | null;
}

export interface RedditImportProgressJob {
  id: number;
  target_type: string;
  target_name: string;
  status: string;
  current_stage: string;
  stage_counts?: Record<string, number> | null;
  progress_percent?: number | null;
  eta_label?: string | null;
  updated_at?: string | null;
}

export interface RedditImportProgressView {
  percent: number;
  stageLabel: string;
  stageRows: Array<{ label: string; value: string; complete: boolean }>;
  metricRows: Array<{ label: string; value: string }>;
  etaLabel: string;
  lastUpdatedLabel: string;
  isStale: boolean;
  warnings: string[];
}

export function buildRedditArchiveImportPlan(
  targetType: RedditArchiveTargetType,
  targetName: string,
  includePosts: boolean,
  includeComments: boolean,
): RedditArchiveImportPlanItem[] {
  const normalizedName = normalizeRedditTargetName(targetName);
  if (!normalizedName) {
    throw new Error("Enter a subreddit or user name.");
  }
  if (!includePosts && !includeComments) {
    throw new Error("Choose posts, comments, or both.");
  }
  const prefix = targetType === "user" ? "u" : "r";
  const stem = `${prefix}_${normalizedName}`;
  const items: RedditArchiveImportPlanItem[] = [];
  if (includePosts) {
    items.push({ kind: "post", path: `data\\${stem}_posts.jsonl` });
  }
  if (includeComments) {
    items.push({ kind: "comment", path: `data\\${stem}_comments.jsonl` });
  }
  return items;
}

export function formatUtcDateRange(minCreatedUtc?: number | null, maxCreatedUtc?: number | null): string {
  const minDate = formatUtcDate(minCreatedUtc);
  const maxDate = formatUtcDate(maxCreatedUtc);
  if (!minDate && !maxDate) {
    return "unknown";
  }
  if (minDate && maxDate && minDate !== maxDate) {
    return `${minDate} to ${maxDate}`;
  }
  return minDate || maxDate || "unknown";
}

export function compactListPreview(values: unknown, maxItems = 3, emptyText = "none"): string {
  const items = normalizeList(values);
  if (items.length === 0) {
    return emptyText;
  }
  if (items.length <= maxItems) {
    return items.join(", ");
  }
  return `${items.slice(0, maxItems).join(", ")} +${items.length - maxItems} more`;
}

export function sourceFilePreview(values: unknown, maxItems = 2, emptyText = "none"): string {
  const items = normalizeList(values).map((value) => value.split(/[\\/]/).filter(Boolean).at(-1) || value);
  return compactListPreview(items, maxItems, emptyText);
}

export function formatPurgeSummary(deleted: Record<string, number>): string {
  const corpusEmbeddings = deleted.corpus_embeddings ?? 0;
  const archiveItems = deleted.reddit_items ?? 0;
  const archiveChunks = deleted.archive_semantic_chunks ?? 0;
  return `Purged ${formatInteger(archiveItems)} Reddit items, ${formatInteger(archiveChunks)} archive chunks, and ${formatInteger(
    corpusEmbeddings,
  )} corpus embeddings.`;
}

export function formatClearStartedMessage(subreddit: string, itemCount?: number | null): string {
  const count = typeof itemCount === "number" && Number.isFinite(itemCount) ? ` ${formatInteger(itemCount)} indexed items` : " indexed data";
  return `Clearing r/${subreddit}${count}. This can take a minute for large archives.`;
}

export function archivePipelineMessage(summary: ArchivePipelineSummary): string {
  const rows = formatInteger(summary.items);
  const metadata = `${formatInteger(summary.metadata_items ?? 0)} / ${rows}`;
  const classifierSkipped = `${formatInteger(summary.classifier_skipped_items ?? summary.classifier_items ?? 0)} / ${rows}`;
  const importStatus = summary.active_import_status;
  if (importStatus === "queued" || importStatus === "running") {
    return `${rows} rows imported. Metadata ${metadata}. Classifier skipped ${classifierSkipped}. Import ${importStatus}.`;
  }
  if ((summary.semantic_index_state ?? "") === "not_built" || summary.semantic_chunks === 0) {
    return `${rows} rows imported. Metadata ${metadata}. Classifier skipped ${classifierSkipped}. Semantic index not built.`;
  }
  if ((summary.semantic_index_state ?? "") === "partial" || summary.embedded_items < summary.items) {
    return `${rows} rows imported. Metadata ${metadata}. Classifier skipped ${classifierSkipped}. Semantic index partial.`;
  }
  return `${rows} rows imported. Metadata ${metadata}. Classifier skipped ${classifierSkipped}. Semantic index ready.`;
}

export function deriveRedditImportProgress(
  job: RedditImportProgressJob,
  _coverage: unknown = null,
  nowMs = Date.now(),
): RedditImportProgressView {
  const counts = job.stage_counts ?? {};
  const stages = [
    { key: "checking_embedding_model", label: "Checking embedding model", count: null, total: null, weight: 2 },
    { key: "downloading", label: "Downloading Reddit rows", count: "downloaded_items", total: "download_total", weight: 13 },
    { key: "importing", label: "Importing rows into SQLite", count: "imported_rows", total: "import_total", weight: 18 },
    { key: "metadata", label: "Writing deterministic metadata", count: "metadata_rows", total: "metadata_total", weight: 17 },
    { key: "chunking", label: "Building semantic chunks", count: "chunked_items", total: "chunk_total", weight: 20 },
    { key: "embedding", label: "Embedding semantic chunks", count: "embedded_chunks", total: "embedding_total", weight: 30 },
  ] as const;
  const stageIndex = Math.max(0, stages.findIndex((stage) => stage.key === job.current_stage));
  const activeStage = stages[stageIndex] ?? stages[0];
  const stageLabel = job.status === "completed" ? "Completed" : activeStage.label;
  const completedWeight = stages.slice(0, stageIndex).reduce((total, stage) => total + stage.weight, 0);
  const stageRatio = activeStage.count && activeStage.total ? ratio(counts[activeStage.count], counts[activeStage.total]) : 0;
  const calculatedPercent = job.status === "completed" ? 100 : Math.min(99, Math.round(completedWeight + activeStage.weight * stageRatio));
  const updatedAtMs = parseTimestampMs(job.updated_at);
  const staleSeconds = updatedAtMs ? Math.floor((nowMs - updatedAtMs) / 1000) : 0;
  const isActive = job.status === "queued" || job.status === "running";
  const isStale = isActive && staleSeconds > 15;
  const warnings = isStale ? [`No backend progress update for ${staleSeconds} seconds.`] : [];

  return {
    percent: Math.max(0, Math.min(100, calculatedPercent)),
    stageLabel,
    stageRows: stages.slice(1).map((stage) => {
      const count = stage.count ? counts[stage.count] : 0;
      const total = stage.total ? counts[stage.total] : 0;
      return {
        label: stage.label,
        value: formatCountPair(count, total),
        complete: ratio(count, total) >= 1,
      };
    }),
    metricRows: [
      { label: "Downloaded rows", value: formatCountPair(counts.downloaded_items, counts.download_total) },
      { label: "Imported rows", value: formatCountPair(counts.imported_rows, counts.import_total) },
      { label: "Metadata rows", value: formatCountPair(counts.metadata_rows, counts.metadata_total) },
      { label: "Classifier skipped", value: formatCountPair(counts.classifier_skipped_items, counts.metadata_total) },
      { label: "Semantic chunks", value: formatCountPair(counts.semantic_chunks, counts.embedding_total || counts.semantic_chunks) },
      { label: "Embedded chunks", value: formatCountPair(counts.embedded_chunks, counts.embedding_total) },
    ],
    etaLabel: job.eta_label || "estimating",
    lastUpdatedLabel: updatedAtMs ? `${Math.max(0, staleSeconds)}s ago` : "unknown",
    isStale,
    warnings,
  };
}

export function openArchiveExportUrl(openUrl: string, opener: BrowserOpener | undefined = defaultOpener()): boolean {
  const url = openUrl.trim();
  if (!url || !opener) {
    return false;
  }
  const opened = opener(url, "_blank", "noopener,noreferrer");
  return opened !== null;
}

function formatInteger(value: number): string {
  return new Intl.NumberFormat().format(value);
}

function formatCountPair(value: unknown, total: unknown): string {
  const current = finiteNumber(value);
  const totalValue = finiteNumber(total);
  if (totalValue > 0) {
    return `${formatInteger(current)} / ${formatInteger(totalValue)}`;
  }
  return formatInteger(current);
}

function ratio(value: unknown, total: unknown): number {
  const totalValue = finiteNumber(total);
  if (totalValue <= 0) {
    return 0;
  }
  return Math.max(0, Math.min(1, finiteNumber(value) / totalValue));
}

function finiteNumber(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? Math.max(0, value) : 0;
}

function parseTimestampMs(value?: string | null): number {
  if (!value) {
    return 0;
  }
  const parsed = Date.parse(value.includes("T") ? value : value.replace(" ", "T"));
  return Number.isFinite(parsed) ? parsed : 0;
}

function formatUtcDate(value?: number | null): string {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return "";
  }
  return new Date(value * 1000).toISOString().slice(0, 10);
}

function normalizeList(values: unknown): string[] {
  if (Array.isArray(values)) {
    return values.map((value) => String(value).trim()).filter(Boolean);
  }
  if (typeof values === "string") {
    return values
      .split(",")
      .map((value) => value.trim())
      .filter(Boolean);
  }
  if (typeof values === "number" && Number.isFinite(values)) {
    return [String(values)];
  }
  return [];
}

function normalizeRedditTargetName(value: string): string {
  return value
    .trim()
    .replace(/^[ru]\//i, "")
    .replace(/[^A-Za-z0-9_]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .toLowerCase();
}

function defaultOpener(): BrowserOpener | undefined {
  return globalThis.window?.open?.bind(globalThis.window) as BrowserOpener | undefined;
}
