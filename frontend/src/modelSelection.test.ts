import { describe, expect, it } from "vitest";

import { chooseChatModelId } from "./modelSelection";
import type { StatusResponse } from "./types";

describe("chooseChatModelId", () => {
  it("keeps the selected chat model when it remains available", () => {
    const status = statusWithModels("default-chat", ["default-chat", "larger-chat"]);

    expect(chooseChatModelId("larger-chat", status)).toBe("larger-chat");
  });

  it("falls back to the configured chat model or first available chat model", () => {
    expect(chooseChatModelId("", statusWithModels("default-chat", ["default-chat", "larger-chat"]))).toBe(
      "default-chat",
    );
    expect(chooseChatModelId("stale-chat", statusWithModels("missing-chat", ["larger-chat"]))).toBe("larger-chat");
  });
});

function statusWithModels(configuredId: string, modelIds: string[]): StatusResponse {
  return {
    lemonade: { reachable: true },
    model: { id: configuredId },
    main_models: modelIds.map((id) => ({ id })),
    vision: { ready: false },
  };
}
