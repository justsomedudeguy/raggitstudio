import { describe, expect, it } from "vitest";

import { parseSseFrames } from "./api";

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
