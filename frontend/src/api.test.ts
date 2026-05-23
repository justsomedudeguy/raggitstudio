import { afterEach, describe, expect, it, vi } from "vitest";

import { fetchArchiveClearJob, parseSseFrames, startArchiveSubredditClear, updateProviderSettings } from "./api";

describe("parseSseFrames", () => {
  it("keeps event names and parses JSON payloads across multiple frames", () => {
    const frames = parseSseFrames(
      'event: reasoning\ndata: {"text":"why"}\n\n' +
        'event: content\ndata: {"text":"answer"}\n\n' +
        'event: conversation\ndata: {"id":4}\n\n' +
        "event: done\ndata: {}\n\n",
    );

    expect(frames).toEqual([
      { event: "reasoning", data: { text: "why" } },
      { event: "content", data: { text: "answer" } },
      { event: "conversation", data: { id: 4 } },
      { event: "done", data: {} },
    ]);
  });

  it("parses conversation frames used to save chat memory", () => {
    const frames = parseSseFrames('event: conversation\ndata: {"id":42}\n\n');

    expect(frames).toEqual([{ event: "conversation", data: { id: 42 } }]);
  });
});

describe("updateProviderSettings", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("sends the OpenAI-compatible base URL to provider settings", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ lemonade: { base_url: "http://new.example/v1", reachable: true } }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );

    const response = await updateProviderSettings("http://new.example/v1");

    expect(response.lemonade?.base_url).toBe("http://new.example/v1");
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/provider-settings",
      expect.objectContaining({
        method: "PUT",
        body: JSON.stringify({ lemonade_base_url: "http://new.example/v1" }),
      }),
    );
  });
});

describe("archive clear jobs", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("starts subreddit clearing in background mode", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ id: "job-1", subreddit: "theehive", status: "queued" }), {
        status: 202,
        headers: { "Content-Type": "application/json" },
      }),
    );

    const response = await startArchiveSubredditClear("theehive");

    expect(response.id).toBe("job-1");
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/archives/subreddits/theehive?background=true",
      expect.objectContaining({ method: "DELETE" }),
    );
  });

  it("fetches a persisted clear job by id", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ id: "job-1", subreddit: "theehive", status: "running" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );

    const response = await fetchArchiveClearJob("job-1");

    expect(response.status).toBe("running");
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/archive-clear-jobs/job-1",
      expect.objectContaining({ headers: expect.any(Headers) }),
    );
  });
});
