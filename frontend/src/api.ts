import type {
  ArchiveCountRequest,
  ArchiveCountResult,
  ArchiveImportPayload,
  ArchiveImportResult,
  ArchiveListResponse,
  ArchiveSearchRequest,
  ArchiveSearchResponse,
  ChatStreamRequest,
  ConversationDetail,
  ConversationMessage,
  ConversationRecord,
  DocumentRecord,
  IngestPayload,
  IngestionResult,
  RagSearchRequest,
  RagSearchResponse,
  ScreenshotCapture,
  SourceRecord,
  SseFrame,
  StatusResponse,
} from "./types";

const FALLBACK_API_BASE_URL = "http://127.0.0.1:8000";

export const API_BASE_URL = readApiBaseUrl();

export class ApiError extends Error {
  status: number;
  details: unknown;

  constructor(message: string, status: number, details: unknown = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.details = details;
  }
}

export function parseSseFrames(input: string): SseFrame[] {
  const normalized = input.replace(/\r\n/g, "\n");
  const rawFrames = normalized.split("\n\n").filter((frame) => frame.trim().length > 0);
  return rawFrames.map(parseSseFrame);
}

export async function fetchStatus(): Promise<StatusResponse> {
  return apiFetch<StatusResponse>("/api/status");
}

export async function ingestSource(payload: IngestPayload): Promise<IngestionResult> {
  return apiFetch<IngestionResult>("/api/ingest", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function fetchSources(): Promise<SourceRecord[]> {
  const response = await apiFetch<{ sources: SourceRecord[] }>("/api/sources");
  return response.sources;
}

export async function fetchDocuments(): Promise<DocumentRecord[]> {
  const response = await apiFetch<{ documents: DocumentRecord[] }>("/api/documents");
  return response.documents;
}

export async function fetchConversations(): Promise<ConversationRecord[]> {
  const response = await apiFetch<{ conversations: ConversationRecord[] }>("/api/conversations");
  return response.conversations;
}

export async function createConversation(payload: {
  title?: string;
  system_prompt?: string;
} = {}): Promise<ConversationRecord> {
  return apiFetch<ConversationRecord>("/api/conversations", {
    method: "POST",
    body: JSON.stringify({
      title: payload.title ?? "New chat",
      system_prompt: payload.system_prompt ?? "",
    }),
  });
}

export async function updateConversation(
  conversationId: number,
  payload: { title?: string; system_prompt?: string },
): Promise<ConversationRecord> {
  return apiFetch<ConversationRecord>(`/api/conversations/${conversationId}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export async function fetchConversation(conversationId: number): Promise<ConversationDetail> {
  return apiFetch<ConversationDetail>(`/api/conversations/${conversationId}`);
}

export async function addConversationMessage(
  conversationId: number,
  payload: { role: ConversationMessage["role"]; content: string; reasoning?: string },
): Promise<{ id: number }> {
  return apiFetch<{ id: number }>(`/api/conversations/${conversationId}/messages`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function searchRag(payload: RagSearchRequest): Promise<RagSearchResponse> {
  return apiFetch<RagSearchResponse>("/api/rag/search", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function fetchArchives(): Promise<ArchiveListResponse> {
  return apiFetch<ArchiveListResponse>("/api/archives");
}

export async function importArchive(payload: ArchiveImportPayload): Promise<ArchiveImportResult> {
  return apiFetch<ArchiveImportResult>("/api/archives/import", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function countArchive(payload: ArchiveCountRequest): Promise<ArchiveCountResult> {
  return apiFetch<ArchiveCountResult>("/api/archives/count", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function searchArchive(payload: ArchiveSearchRequest): Promise<ArchiveSearchResponse> {
  return apiFetch<ArchiveSearchResponse>("/api/archives/search", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function captureScreenshot(monitorIndex = 1): Promise<ScreenshotCapture> {
  return apiFetch<ScreenshotCapture>("/api/screenshots/capture", {
    method: "POST",
    body: JSON.stringify({ monitor_index: monitorIndex }),
  });
}

export async function streamChat(
  payload: ChatStreamRequest,
  onFrame: (frame: SseFrame) => void,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch(`${API_BASE_URL}/api/chat/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
    signal,
  });

  if (!response.ok) {
    throw await toApiError(response);
  }

  if (!response.body) {
    throw new ApiError("The chat stream response did not include a readable body.", response.status);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) {
      break;
    }
    buffer += decoder.decode(value, { stream: true });
    const parts = buffer.replace(/\r\n/g, "\n").split("\n\n");
    buffer = parts.pop() ?? "";
    for (const part of parts) {
      for (const frame of parseSseFrames(`${part}\n\n`)) {
        onFrame(frame);
      }
    }
  }

  buffer += decoder.decode();
  if (buffer.trim()) {
    for (const frame of parseSseFrames(buffer)) {
      onFrame(frame);
    }
  }
}

async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...init,
    headers,
  });

  if (!response.ok) {
    throw await toApiError(response);
  }

  return (await response.json()) as T;
}

async function toApiError(response: Response): Promise<ApiError> {
  let details: unknown = null;
  let message = `Request failed with HTTP ${response.status}`;
  try {
    details = await response.json();
    if (isRecord(details) && typeof details.detail === "string") {
      message = details.detail;
    }
  } catch {
    const text = await response.text().catch(() => "");
    if (text) {
      message = text;
      details = text;
    }
  }
  return new ApiError(message, response.status, details);
}

function parseSseFrame(frameText: string): SseFrame {
  const lines = frameText.replace(/\r\n/g, "\n").split("\n");
  let event = "message";
  const dataLines: string[] = [];

  for (const line of lines) {
    if (!line || line.startsWith(":")) {
      continue;
    }
    const separatorIndex = line.indexOf(":");
    const field = separatorIndex === -1 ? line : line.slice(0, separatorIndex);
    const value = separatorIndex === -1 ? "" : line.slice(separatorIndex + 1).replace(/^ /, "");
    if (field === "event") {
      event = value;
    }
    if (field === "data") {
      dataLines.push(value);
    }
  }

  const dataText = dataLines.join("\n");
  if (!dataText) {
    return { event, data: null };
  }

  try {
    return { event, data: JSON.parse(dataText) as unknown };
  } catch {
    return { event, data: dataText };
  }
}

function readApiBaseUrl(): string {
  const meta = import.meta as ImportMeta & { env?: Record<string, string | undefined> };
  const configured = meta.env?.VITE_API_BASE_URL?.trim();
  return (configured || FALLBACK_API_BASE_URL).replace(/\/+$/, "");
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
