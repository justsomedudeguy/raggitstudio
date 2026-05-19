export type ChatMode = "thinking" | "direct" | "deep-rag";
export type ChatRole = "system" | "user" | "assistant";

export type JsonRecord = Record<string, unknown>;

export interface ChatApiMessage {
  role: ChatRole;
  content: string;
}

export interface ChatStreamRequest {
  messages: ChatApiMessage[];
  mode: ChatMode;
  model_id?: string;
  collection_ids?: number[];
  query?: string;
  tools_enabled?: boolean;
  conversation_id?: number;
  system_prompt?: string;
}

export interface SseFrame<T = unknown> {
  event: string;
  data: T;
}

export interface IngestPayload {
  kind: "text" | "file" | "folder" | "url" | "crawl" | "jsonl";
  title?: string;
  text?: string;
  path?: string;
  url?: string;
  max_pages?: number;
  uri?: string;
}

export interface IngestionResult {
  job_id: number;
  status: string;
  processed_count: number;
  failed_count: number;
  log: string;
}

export interface ArchiveImportPayload {
  path: string;
  kind?: "post" | "comment" | "";
}

export interface ArchiveImportResult {
  file_id: number;
  path: string;
  file_format: string;
  kind?: string | null;
  status: string;
  indexed_count: number;
  failed_count: number;
  log: string;
}

export interface ArchiveCoverage {
  files: number;
  items: number;
  posts: number;
  comments: number;
  semantic_chunks: number;
  embedded_items: number;
}

export interface ArchiveFileRecord extends JsonRecord {
  id: number;
  path: string;
  file_format: string;
  fallback_kind?: string | null;
  status: string;
  row_count: number;
  indexed_count: number;
  failed_count: number;
  log?: string;
  created_at?: string;
  updated_at?: string;
  finished_at?: string | null;
}

export interface ArchiveCountRequest {
  term: string;
  case_sensitive: boolean;
  kind?: string;
  subreddit?: string;
}

export interface ArchiveCountResult {
  term: string;
  case_sensitive: boolean;
  occurrences: number;
  matched_items: number;
  searched_items: number;
  by_kind: Record<string, number>;
}

export interface ArchiveSearchRequest {
  query: string;
  limit: number;
  kind?: string;
  subreddit?: string;
  after?: number;
  before?: number;
}

export interface ArchiveSearchResult extends JsonRecord {
  id: number;
  kind: string;
  subreddit: string;
  author: string;
  created_utc?: number;
  score?: number;
  title: string;
  selftext: string;
  body: string;
  text: string;
  url: string;
  permalink: string;
  link_id: string;
  parent_id: string;
  raw?: JsonRecord;
  meta?: JsonRecord;
}

export interface ArchiveListResponse {
  files: ArchiveFileRecord[];
  coverage: ArchiveCoverage;
}

export interface ArchiveSearchResponse {
  results: ArchiveSearchResult[];
  coverage: ArchiveCoverage;
}

export interface SourceRecord extends JsonRecord {
  id: number;
  source_type: string;
  uri: string;
  title: string;
  status: string;
  created_at?: string;
  updated_at?: string;
}

export interface DocumentRecord extends JsonRecord {
  id: number;
  source_id: number;
  title: string;
  summary?: string;
  document_type?: string;
  source_type?: string;
  metadata?: JsonRecord;
  created_at?: string;
}

export interface RagSearchRequest {
  query: string;
  top_k: number;
  filters?: JsonRecord;
}

export interface RagResult {
  chunk_id: number;
  citation_id: string;
  text: string;
  metadata?: JsonRecord;
  score: number;
}

export interface RagSearchResponse {
  results: RagResult[];
  packed_context: string;
  retrieval_run_id: number | null;
}

export interface StatusResponse {
  lemonade?: {
    base_url?: string;
    reachable: boolean;
    error?: string;
  };
  model?: {
    id: string;
    effective_id?: string;
    available?: boolean;
    context_size?: number | null;
    backend?: string | null;
    args?: string | null;
  };
  main_models?: ModelOption[];
  embedding?: {
    id: string;
  };
  reranker?: {
    id: string;
  };
  classifier?: {
    id: string;
  };
  vision?: {
    ready: boolean;
    reason?: string;
    message?: string;
  };
  database?: {
    sources: number;
    documents: number;
    chunks: number;
    embeddings: number;
  };
  archive?: ArchiveCoverage;
}

export interface ModelOption {
  id: string;
  labels?: string[];
  context_size?: number | null;
  backend?: string | null;
  args?: string | null;
}

export interface ScreenshotCapture {
  attachment_id: number;
  path: string;
  width: number;
  height: number;
  monitor_index: number;
  captured_at: string;
}

export interface ConversationRecord extends JsonRecord {
  id: number;
  title: string;
  system_prompt: string;
  created_at?: string;
  updated_at?: string;
}

export interface ConversationMessage extends JsonRecord {
  id: number;
  conversation_id: number;
  role: ChatRole;
  content: string;
  reasoning: string;
  metadata?: JsonRecord;
  created_at?: string;
}

export interface ConversationDetail {
  conversation: ConversationRecord;
  messages: ConversationMessage[];
}
