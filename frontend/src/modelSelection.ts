import type { StatusResponse } from "./types";

export function chooseChatModelId(currentModelId: string, status: StatusResponse | null): string {
  const modelIds = (status?.main_models ?? []).map((model) => model.id).filter(Boolean);
  if (currentModelId && modelIds.includes(currentModelId)) {
    return currentModelId;
  }
  const configuredId = status?.model?.id ?? "";
  if (configuredId && modelIds.includes(configuredId)) {
    return configuredId;
  }
  return modelIds[0] ?? configuredId;
}
