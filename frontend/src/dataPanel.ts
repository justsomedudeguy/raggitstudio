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
  semantic_chunks: number;
  embedded_items: number;
  semantic_index_state?: string | null;
  resumable_import?: boolean | null;
  active_import_status?: string | null;
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
  const classifier = `${formatInteger(summary.classifier_items ?? 0)} / ${rows}`;
  const importStatus = summary.active_import_status;
  if (importStatus === "queued" || importStatus === "running") {
    return `${rows} rows imported. Metadata ${metadata}. Classifier ${classifier}. Import ${importStatus}.`;
  }
  if (summary.resumable_import) {
    return `${rows} rows imported. Metadata ${metadata}. Classifier ${classifier}. Import interrupted; resume available.`;
  }
  if ((summary.semantic_index_state ?? "") === "not_built" || summary.semantic_chunks === 0) {
    return `${rows} rows imported. Metadata ${metadata}. Classifier ${classifier}. Semantic index not built.`;
  }
  if ((summary.semantic_index_state ?? "") === "partial" || summary.embedded_items < summary.items) {
    return `${rows} rows imported. Metadata ${metadata}. Classifier ${classifier}. Semantic index partial.`;
  }
  return `${rows} rows imported. Metadata ${metadata}. Classifier ${classifier}. Semantic index ready.`;
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
