import { describe, expect, it, vi } from "vitest";

import {
  archivePipelineMessage,
  buildRedditArchiveImportPlan,
  compactListPreview,
  deriveRedditImportProgress,
  formatClearStartedMessage,
  formatPurgeSummary,
  formatUtcDateRange,
  openArchiveExportUrl,
  sourceFilePreview,
} from "./dataPanel";

describe("data panel helpers", () => {
  it("formats archive coverage date ranges from unix seconds", () => {
    expect(formatUtcDateRange(1_704_067_200, 1_704_326_400)).toBe("2024-01-01 to 2024-01-04");
    expect(formatUtcDateRange(1_704_067_200, 1_704_067_200)).toBe("2024-01-01");
  });

  it("keeps list previews compact while preserving the total", () => {
    expect(compactListPreview(["comments.jsonl", "posts.jsonl", "embeddings.jsonl"], 2, "none")).toBe(
      "comments.jsonl, posts.jsonl +1 more",
    );
    expect(compactListPreview([], 2, "none")).toBe("none");
  });

  it("shows source file names instead of full local paths", () => {
    expect(
      sourceFilePreview([
        "C:\\Users\\Grisha\\Documents\\CustomChat\\data\\r_theehive_comments.jsonl",
        "C:\\Users\\Grisha\\Documents\\CustomChat\\data\\r_theehive_posts.jsonl",
      ]),
    ).toBe("r_theehive_comments.jsonl, r_theehive_posts.jsonl");
  });

  it("summarizes purged archive and corpus rows", () => {
    expect(
      formatPurgeSummary({
        reddit_items: 107476,
        archive_semantic_chunks: 12,
        corpus_embeddings: 467,
      }),
    ).toBe("Purged 107,476 Reddit items, 12 archive chunks, and 467 corpus embeddings.");
  });

  it("formats a clear-data progress message for large archives", () => {
    expect(formatClearStartedMessage("theehive", 107476)).toBe(
      "Clearing r/theehive 107,476 indexed items. This can take a minute for large archives.",
    );
  });

  it("describes imported archives with no semantic index as not built", () => {
    expect(
      archivePipelineMessage({
        items: 23216,
        metadata_items: 23132,
        classifier_items: 5592,
        semantic_chunks: 0,
        embedded_items: 0,
        semantic_index_state: "not_built",
      }),
    ).toBe("23,216 rows imported. Metadata 23,132 / 23,216. Classifier skipped 5,592 / 23,216. Semantic index not built.");
  });

  it("does not describe interrupted archive imports as resumable", () => {
    expect(
      archivePipelineMessage({
        items: 23216,
        metadata_items: 23132,
        classifier_items: 5592,
        semantic_chunks: 0,
        embedded_items: 0,
        semantic_index_state: "not_built",
        resumable_import: true,
      }),
    ).toBe("23,216 rows imported. Metadata 23,132 / 23,216. Classifier skipped 5,592 / 23,216. Semantic index not built.");
  });

  it("derives accurate single-shot import progress from confirmed counters", () => {
    const progress = deriveRedditImportProgress(
      {
        id: 7,
        target_type: "subreddit",
        target_name: "theehive",
        status: "running",
        current_stage: "embedding",
        stage_counts: {
          downloaded_items: 200,
          download_total: 200,
          imported_rows: 200,
          import_total: 200,
          metadata_rows: 200,
          metadata_total: 200,
          classifier_skipped_items: 200,
          semantic_chunks: 320,
          chunk_total: 200,
          chunked_items: 200,
          embedded_chunks: 160,
          embedding_total: 320,
        },
        progress_percent: 0,
        eta_label: "about 4 min",
        updated_at: "2026-05-25T01:00:00Z",
      },
      null,
      Date.parse("2026-05-25T01:00:05Z"),
    );

    expect(progress.percent).toBe(85);
    expect(progress.stageLabel).toBe("Embedding semantic chunks");
    expect(progress.isStale).toBe(false);
    expect(progress.metricRows).toContainEqual({ label: "Classifier skipped", value: "200 / 200" });
    expect(progress.metricRows).toContainEqual({ label: "Embedded chunks", value: "160 / 320" });
  });

  it("flags stale single-shot import progress when the backend stops updating", () => {
    const progress = deriveRedditImportProgress(
      {
        id: 8,
        target_type: "subreddit",
        target_name: "theehive",
        status: "running",
        current_stage: "metadata",
        stage_counts: {
          imported_rows: 100,
          import_total: 100,
          metadata_rows: 20,
          metadata_total: 100,
        },
        progress_percent: 0,
        eta_label: "estimating",
        updated_at: "2026-05-25T01:00:00Z",
      },
      null,
      Date.parse("2026-05-25T01:00:16Z"),
    );

    expect(progress.isStale).toBe(true);
    expect(progress.warnings).toContain("No backend progress update for 16 seconds.");
  });

  it("builds local Arctic Shift dump imports from the selected subreddit", () => {
    expect(buildRedditArchiveImportPlan("subreddit", "TheeHive", true, true)).toEqual([
      { kind: "post", path: "data\\r_theehive_posts.jsonl" },
      { kind: "comment", path: "data\\r_theehive_comments.jsonl" },
    ]);
  });

  it("rejects archive imports without a target or selected dump type", () => {
    expect(() => buildRedditArchiveImportPlan("subreddit", "", true, true)).toThrow("Enter a subreddit or user name.");
    expect(() => buildRedditArchiveImportPlan("subreddit", "theehive", false, false)).toThrow(
      "Choose posts, comments, or both.",
    );
  });

  it("opens exported archive URLs in a new tab with opener isolation", () => {
    const opener = vi.fn();

    expect(openArchiveExportUrl("file:///C:/archive/index.html", opener)).toBe(true);

    expect(opener).toHaveBeenCalledWith("file:///C:/archive/index.html", "_blank", "noopener,noreferrer");
  });

  it("reports blocked archive export opens", () => {
    expect(openArchiveExportUrl("file:///C:/archive/index.html", () => null)).toBe(false);
    expect(openArchiveExportUrl("", vi.fn())).toBe(false);
  });
});
