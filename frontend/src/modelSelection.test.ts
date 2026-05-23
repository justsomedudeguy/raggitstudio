import { describe, expect, it } from "vitest";

import { chooseChatModelId } from "./modelSelection";
import type { StatusResponse } from "./types";

describe("chooseChatModelId", () => {
  it("keeps the selected chat model when it remains available", () => {
    const status = statusWithModels("default-chat", ["default-chat", "larger-chat"]);

    expect(chooseChatModelId("larger-chat", status)).toBe("larger-chat");
  });

  it("does not choose a model before the user explicitly selects one", () => {
    expect(chooseChatModelId("", statusWithModels("default-chat", ["default-chat", "larger-chat"]))).toBe("");
  });

  it("clears stale selected models instead of falling back to another option", () => {
    expect(chooseChatModelId("stale-chat", statusWithModels("default-chat", ["default-chat", "larger-chat"]))).toBe("");
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
