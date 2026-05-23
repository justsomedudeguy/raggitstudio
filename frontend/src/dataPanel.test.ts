import { describe, expect, it, vi } from "vitest";

import {
  buildRedditArchiveImportPlan,
  compactListPreview,
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
