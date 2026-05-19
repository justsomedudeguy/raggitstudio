import { FormEvent, useEffect, useMemo, useState } from "react";
import type { LucideIcon } from "lucide-react";
import {
  Activity,
  Archive,
  Brain,
  Camera,
  CheckCircle2,
  CircleAlert,
  Clock3,
  Database,
  FileText,
  FolderOpen,
  Globe,
  Hash as HashIcon,
  Loader2,
  MessageSquare,
  Plus,
  RefreshCw,
  Save,
  Search,
  Send,
  Square,
  Upload,
  Wrench,
  XCircle,
} from "lucide-react";

import {
  API_BASE_URL,
  captureScreenshot,
  countArchive,
  createConversation,
  fetchArchives,
  fetchConversation,
  fetchConversations,
  fetchDocuments,
  fetchSources,
  fetchStatus,
  importArchive,
  ingestSource,
  searchArchive,
  searchRag,
  streamChat,
  updateConversation,
} from "./api";
import { chooseChatModelId } from "./modelSelection";
import type {
  ArchiveCountResult,
  ArchiveCoverage,
  ArchiveFileRecord,
  ArchiveImportResult,
  ArchiveSearchResult,
  ChatApiMessage,
  ChatMode,
  ConversationMessage,
  ConversationRecord,
  DocumentRecord,
  IngestPayload,
  IngestionResult,
  JsonRecord,
  ModelOption,
  RagResult,
  RagSearchResponse,
  ScreenshotCapture,
  SourceRecord,
  SseFrame,
  StatusResponse,
} from "./types";

type ViewKey = "chat" | "corpus" | "archives" | "retrieval" | "status";
type MessageStatus = "streaming" | "done" | "error";

interface ChatEvent {
  id: string;
  event: string;
  data: unknown;
}

interface UiMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  reasoning: string;
  events: ChatEvent[];
  status: MessageStatus;
  error?: string;
  mode?: ChatMode;
  ragQuery?: string;
  createdAt: string;
}

interface NavItem {
  key: ViewKey;
  label: string;
  icon: LucideIcon;
}

const NAV_ITEMS: NavItem[] = [
  { key: "chat", label: "Chat", icon: MessageSquare },
  { key: "corpus", label: "Corpus", icon: Database },
  { key: "archives", label: "Archives", icon: Archive },
  { key: "retrieval", label: "Retrieval Lab", icon: Search },
  { key: "status", label: "Status", icon: Activity },
];

function App() {
  const [activeView, setActiveView] = useState<ViewKey>("chat");
  const [status, setStatus] = useState<StatusResponse | null>(null);
  const [statusLoading, setStatusLoading] = useState(false);
  const [statusError, setStatusError] = useState("");
  const [captures, setCaptures] = useState<ScreenshotCapture[]>([]);
  const [captureLoading, setCaptureLoading] = useState(false);
  const [captureError, setCaptureError] = useState("");

  async function refreshStatus() {
    setStatusLoading(true);
    setStatusError("");
    try {
      setStatus(await fetchStatus());
    } catch (error) {
      setStatusError(errorMessage(error));
    } finally {
      setStatusLoading(false);
    }
  }

  async function handleCapture() {
    setCaptureLoading(true);
    setCaptureError("");
    try {
      const capture = await captureScreenshot(1);
      setCaptures((current) => [capture, ...current].slice(0, 5));
    } catch (error) {
      setCaptureError(errorMessage(error));
    } finally {
      setCaptureLoading(false);
    }
  }

  useEffect(() => {
    void refreshStatus();
  }, []);

  return (
    <div className="workbench">
      <aside className="nav">
        <div className="brand">
          <div className="brand-mark">Q</div>
          <div>
            <div className="brand-title">CustomChat</div>
            <div className="brand-subtitle">Local RAG workbench</div>
          </div>
        </div>
        <nav className="nav-list" aria-label="Workbench views">
          {NAV_ITEMS.map((item) => {
            const Icon = item.icon;
            return (
              <button
                key={item.key}
                type="button"
                className={`nav-item ${activeView === item.key ? "active" : ""}`}
                onClick={() => setActiveView(item.key)}
              >
                <Icon size={17} />
                <span>{item.label}</span>
              </button>
            );
          })}
        </nav>
        <div className="api-box">
          <div className="label">API base</div>
          <code>{API_BASE_URL}</code>
        </div>
        <div className="attribution">
          Archive tooling based on{" "}
          <a href="https://github.com/ArthurHeitmann/arctic_shift" target="_blank" rel="noreferrer">
            Arthur Heitmann&apos;s Arctic Shift
          </a>
          .
        </div>
      </aside>

      <main className="main">
        {activeView === "chat" && (
          <ChatView status={status} statusLoading={statusLoading} onRefreshStatus={refreshStatus} />
        )}
        {activeView === "corpus" && <CorpusView />}
        {activeView === "archives" && <ArchivesView onRefreshStatus={refreshStatus} />}
        {activeView === "retrieval" && <RetrievalLab />}
        {activeView === "status" && (
          <StatusView
            status={status}
            loading={statusLoading}
            error={statusError}
            captures={captures}
            captureLoading={captureLoading}
            captureError={captureError}
            onRefresh={refreshStatus}
            onCapture={handleCapture}
          />
        )}
      </main>

      <StatusInspector
        status={status}
        loading={statusLoading}
        error={statusError}
        captures={captures}
        captureLoading={captureLoading}
        captureError={captureError}
        onRefresh={refreshStatus}
        onCapture={handleCapture}
      />
    </div>
  );
}

function ChatView({
  status,
  statusLoading,
  onRefreshStatus,
}: {
  status: StatusResponse | null;
  statusLoading: boolean;
  onRefreshStatus: () => void | Promise<void>;
}) {
  const [conversations, setConversations] = useState<ConversationRecord[]>([]);
  const [activeConversationId, setActiveConversationId] = useState<number | null>(null);
  const [messages, setMessages] = useState<UiMessage[]>([]);
  const [systemPrompt, setSystemPrompt] = useState("");
  const [draft, setDraft] = useState("");
  const [mode, setMode] = useState<ChatMode>("thinking");
  const [selectedModelId, setSelectedModelId] = useState("");
  const [ragQuery, setRagQuery] = useState("");
  const [toolsEnabled, setToolsEnabled] = useState(true);
  const [streamController, setStreamController] = useState<AbortController | null>(null);
  const [error, setError] = useState("");
  const [historyLoading, setHistoryLoading] = useState(false);
  const [savingPrompt, setSavingPrompt] = useState(false);

  const mainModels = status?.main_models ?? [];
  const selectedChatModelId = chooseChatModelId(selectedModelId, status);
  const isStreaming = streamController !== null;
  const canSend = draft.trim().length > 0 && !isStreaming && selectedChatModelId.length > 0;

  const apiMessages = useMemo<ChatApiMessage[]>(
    () =>
      messages
        .filter((message) => message.content.trim().length > 0)
        .map((message) => ({ role: message.role, content: message.content })),
    [messages],
  );

  async function refreshConversations(nextActiveId = activeConversationId) {
    setHistoryLoading(true);
    try {
      const nextConversations = await fetchConversations();
      setConversations(nextConversations);
      if (nextActiveId && nextConversations.some((item) => item.id === nextActiveId)) {
        setActiveConversationId(nextActiveId);
      }
    } catch (historyError) {
      setError(errorMessage(historyError));
    } finally {
      setHistoryLoading(false);
    }
  }

  async function loadConversation(conversationId: number) {
    setError("");
    setHistoryLoading(true);
    try {
      const detail = await fetchConversation(conversationId);
      setActiveConversationId(detail.conversation.id);
      setSystemPrompt(detail.conversation.system_prompt || "");
      setMessages(detail.messages.map(messageFromRecord));
    } catch (loadError) {
      setError(errorMessage(loadError));
    } finally {
      setHistoryLoading(false);
    }
  }

  async function startNewConversation() {
    setError("");
    try {
      const conversation = await createConversation({ title: "New chat", system_prompt: systemPrompt });
      setActiveConversationId(conversation.id);
      setMessages([]);
      await refreshConversations(conversation.id);
    } catch (createError) {
      setError(errorMessage(createError));
    }
  }

  async function saveSystemPrompt() {
    if (!activeConversationId) {
      await startNewConversation();
      return;
    }
    setSavingPrompt(true);
    setError("");
    try {
      await updateConversation(activeConversationId, { system_prompt: systemPrompt });
      await refreshConversations(activeConversationId);
    } catch (saveError) {
      setError(errorMessage(saveError));
    } finally {
      setSavingPrompt(false);
    }
  }

  async function submitChat(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canSend) {
      return;
    }

    const prompt = draft.trim();
    const query = mode === "deep-rag" ? ragQuery.trim() || prompt : ragQuery.trim();
    const userMessage: UiMessage = {
      id: makeId("msg"),
      role: "user",
      content: prompt,
      reasoning: "",
      events: [],
      status: "done",
      mode,
      ragQuery: query || undefined,
      createdAt: new Date().toISOString(),
    };
    const assistantId = makeId("msg");
    const assistantMessage: UiMessage = {
      id: assistantId,
      role: "assistant",
      content: "",
      reasoning: "",
      events: [],
      status: "streaming",
      mode,
      ragQuery: query || undefined,
      createdAt: new Date().toISOString(),
    };
    const controller = new AbortController();

    setMessages((current) => [...current, userMessage, assistantMessage]);
    setDraft("");
    setError("");
    setStreamController(controller);

    let streamedConversationId = activeConversationId;
    try {
      await streamChat(
        {
          messages: [...apiMessages, { role: "user", content: prompt }],
          mode,
          model_id: selectedChatModelId,
          query: query || undefined,
          collection_ids: [],
          tools_enabled: toolsEnabled,
          conversation_id: activeConversationId ?? undefined,
          system_prompt: systemPrompt,
        },
        (frame) => {
          if (frame.event === "conversation" && isRecord(frame.data) && typeof frame.data.id === "number") {
            streamedConversationId = frame.data.id;
            setActiveConversationId(frame.data.id);
          }
          applyChatFrame(assistantId, frame, setMessages);
        },
        controller.signal,
      );
      if (streamedConversationId) {
        await refreshConversations(streamedConversationId);
      }
      setMessages((current) =>
        current.map((message) =>
          message.id === assistantId && message.status === "streaming"
            ? { ...message, status: "done" }
            : message,
        ),
      );
    } catch (streamError) {
      if (isAbortError(streamError)) {
        setMessages((current) =>
          current.map((message) =>
            message.id === assistantId
              ? {
                  ...message,
                  status: "done",
                  events: [
                    ...message.events,
                    { id: makeId("evt"), event: "aborted", data: { message: "Stream stopped by user" } },
                  ],
                }
              : message,
          ),
        );
      } else {
        const message = errorMessage(streamError);
        setError(message);
        setMessages((current) =>
          current.map((item) => (item.id === assistantId ? { ...item, status: "error", error: message } : item)),
        );
      }
    } finally {
      setStreamController(null);
    }
  }

  function stopStream() {
    streamController?.abort();
  }

  useEffect(() => {
    void refreshConversations();
  }, []);

  useEffect(() => {
    setSelectedModelId((current) => chooseChatModelId(current, status));
  }, [status]);

  return (
    <section className="view chat-view">
      <ViewHeader
        eyebrow="Chat"
        title="Qwen3.5 chat memory"
        detail="Persistent conversations, saved system prompt, RAG, and contextual web-search events."
        actions={
          <button type="button" className="button" onClick={startNewConversation}>
            <Plus size={16} />
            New chat
          </button>
        }
      />

      <div className="chat-shell">
        <aside className="history-panel panel">
          <PanelTitle icon={Clock3} title="Chat history" />
          <div className="history-actions">
            <button type="button" className="button" onClick={() => void refreshConversations()} disabled={historyLoading}>
              <RefreshCw size={16} className={historyLoading ? "spin" : ""} />
              Refresh
            </button>
          </div>
          <div className="history-list">
            {conversations.length === 0 ? (
              <EmptyState
                icon={MessageSquare}
                title="No saved chats"
                detail="New conversations appear here after the first send."
                compact
              />
            ) : (
              conversations.map((conversation) => (
                <button
                  key={conversation.id}
                  type="button"
                  className={`history-item ${activeConversationId === conversation.id ? "active" : ""}`}
                  onClick={() => void loadConversation(conversation.id)}
                >
                  <strong>{conversation.title}</strong>
                  <span>{conversation.updated_at || conversation.created_at || `#${conversation.id}`}</span>
                </button>
              ))
            )}
          </div>
        </aside>

        <div className="chat-main-pane">
          <div className="system-prompt panel">
            <label>
              <span>System prompt</span>
              <textarea
                value={systemPrompt}
                onChange={(event) => setSystemPrompt(event.target.value)}
                rows={3}
                placeholder="Saved with the selected chat and sent as the first system message."
              />
            </label>
            <button type="button" className="button" onClick={saveSystemPrompt} disabled={savingPrompt}>
              <Save size={16} />
              Save prompt
            </button>
          </div>

          <div className="chat-log" aria-live="polite">
        {messages.length === 0 ? (
          <EmptyState icon={MessageSquare} title="No conversation yet" detail="Send a prompt to start a local stream." />
        ) : (
          messages.map((message) => <MessageBubble key={message.id} message={message} />)
        )}
      </div>

      {error && <InlineError message={error} />}

      <form className="composer" onSubmit={submitChat}>
        <div className="composer-row">
          <label className="model-field">
            <span>Chat model</span>
            <select
              value={selectedChatModelId}
              onChange={(event) => setSelectedModelId(event.target.value)}
              disabled={isStreaming || mainModels.length === 0}
            >
              {mainModels.length === 0 ? (
                <option value={selectedChatModelId}>{selectedChatModelId || "No chat models found"}</option>
              ) : (
                mainModels.map((model) => (
                  <option key={model.id} value={model.id}>
                    {formatModelOption(model)}
                  </option>
                ))
              )}
            </select>
          </label>
          <button
            type="button"
            className="icon-button"
            onClick={onRefreshStatus}
            disabled={statusLoading || isStreaming}
            title="Refresh chat models"
          >
            <RefreshCw size={16} className={statusLoading ? "spin" : ""} />
          </button>
          <label className="mode-field">
            <span>Mode</span>
            <select value={mode} onChange={(event) => setMode(event.target.value as ChatMode)}>
              <option value="thinking">thinking</option>
              <option value="direct">direct</option>
              <option value="deep-rag">deep-rag</option>
            </select>
          </label>
          <label className="grow">
            <span>Optional RAG query</span>
            <input
              value={ragQuery}
              onChange={(event) => setRagQuery(event.target.value)}
              placeholder="Leave blank to skip retrieval, or let deep-rag use the prompt"
            />
          </label>
          <label className="toggle-row">
            <input
              type="checkbox"
              checked={toolsEnabled}
              onChange={(event) => setToolsEnabled(event.target.checked)}
            />
            <span>Web/RAG tools</span>
          </label>
        </div>
        <div className="composer-row">
          <textarea
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            placeholder="Ask the local model..."
            rows={4}
          />
          <div className="composer-actions">
            {isStreaming ? (
              <button type="button" className="button danger" onClick={stopStream}>
                <Square size={16} />
                Stop
              </button>
            ) : (
              <button type="submit" className="button primary" disabled={!canSend}>
                <Send size={16} />
                Send
              </button>
            )}
          </div>
        </div>
      </form>
        </div>
      </div>
    </section>
  );
}

function CorpusView() {
  const [sources, setSources] = useState<SourceRecord[]>([]);
  const [documents, setDocuments] = useState<DocumentRecord[]>([]);
  const [loading, setLoading] = useState(false);
  const [ingesting, setIngesting] = useState(false);
  const [error, setError] = useState("");
  const [jobResult, setJobResult] = useState<IngestionResult | null>(null);

  const [textTitle, setTextTitle] = useState("");
  const [textUri, setTextUri] = useState("");
  const [textBody, setTextBody] = useState("");
  const [pathKind, setPathKind] = useState<"file" | "folder">("file");
  const [pathValue, setPathValue] = useState("");
  const [importKind, setImportKind] = useState<"url" | "crawl" | "jsonl">("url");
  const [importValue, setImportValue] = useState("");
  const [crawlMaxPages, setCrawlMaxPages] = useState(10);

  async function refreshLists() {
    setLoading(true);
    setError("");
    try {
      const [nextSources, nextDocuments] = await Promise.all([fetchSources(), fetchDocuments()]);
      setSources(nextSources);
      setDocuments(nextDocuments);
    } catch (listError) {
      setError(errorMessage(listError));
    } finally {
      setLoading(false);
    }
  }

  async function runIngest(payload: IngestPayload) {
    setIngesting(true);
    setError("");
    try {
      const result = await ingestSource(payload);
      setJobResult(result);
      await refreshLists();
    } catch (ingestError) {
      setError(errorMessage(ingestError));
    } finally {
      setIngesting(false);
    }
  }

  function submitText(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!textBody.trim()) {
      setError("Text ingest requires content.");
      return;
    }
    void runIngest({
      kind: "text",
      title: textTitle.trim() || undefined,
      uri: textUri.trim() || undefined,
      text: textBody.trim(),
    });
  }

  function submitPath(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!pathValue.trim()) {
      setError("Path ingest requires a local path.");
      return;
    }
    void runIngest({ kind: pathKind, path: pathValue.trim() });
  }

  function submitImport(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!importValue.trim()) {
      setError("Import requires a URL or JSONL path.");
      return;
    }
    if (importKind === "jsonl") {
      void runIngest({ kind: "jsonl", path: importValue.trim() });
      return;
    }
    void runIngest({
      kind: importKind,
      url: importValue.trim(),
      max_pages: importKind === "crawl" ? crawlMaxPages : undefined,
    });
  }

  useEffect(() => {
    void refreshLists();
  }, []);

  return (
    <section className="view">
      <ViewHeader
        eyebrow="Corpus"
        title="Ingestion and source inventory"
        detail="Text, local paths, URLs, crawls, JSONL imports, and the database-backed lists."
        actions={
          <button type="button" className="button" onClick={refreshLists} disabled={loading}>
            <RefreshCw size={16} className={loading ? "spin" : ""} />
            Refresh
          </button>
        }
      />

      {error && <InlineError message={error} />}

      <div className="form-grid">
        <form className="panel form-panel" onSubmit={submitText}>
          <PanelTitle icon={FileText} title="Text ingest" />
          <label>
            <span>Title</span>
            <input value={textTitle} onChange={(event) => setTextTitle(event.target.value)} placeholder="Optional" />
          </label>
          <label>
            <span>URI</span>
            <input value={textUri} onChange={(event) => setTextUri(event.target.value)} placeholder="Optional stable ID" />
          </label>
          <label>
            <span>Text</span>
            <textarea value={textBody} onChange={(event) => setTextBody(event.target.value)} rows={7} />
          </label>
          <button type="submit" className="button primary" disabled={ingesting}>
            <Upload size={16} />
            Ingest text
          </button>
        </form>

        <form className="panel form-panel" onSubmit={submitPath}>
          <PanelTitle icon={FolderOpen} title="File or folder ingest" />
          <label>
            <span>Kind</span>
            <select value={pathKind} onChange={(event) => setPathKind(event.target.value as "file" | "folder")}>
              <option value="file">file</option>
              <option value="folder">folder</option>
            </select>
          </label>
          <label>
            <span>Local path</span>
            <input
              value={pathValue}
              onChange={(event) => setPathValue(event.target.value)}
              placeholder="C:\\path\\to\\file-or-folder"
            />
          </label>
          <button type="submit" className="button primary" disabled={ingesting}>
            <Upload size={16} />
            Ingest path
          </button>
        </form>

        <form className="panel form-panel" onSubmit={submitImport}>
          <PanelTitle icon={Globe} title="URL, crawl, or JSONL import" />
          <label>
            <span>Kind</span>
            <select value={importKind} onChange={(event) => setImportKind(event.target.value as "url" | "crawl" | "jsonl")}>
              <option value="url">url</option>
              <option value="crawl">crawl</option>
              <option value="jsonl">jsonl</option>
            </select>
          </label>
          <label>
            <span>{importKind === "jsonl" ? "JSONL path" : "URL"}</span>
            <input
              value={importValue}
              onChange={(event) => setImportValue(event.target.value)}
              placeholder={importKind === "jsonl" ? "C:\\path\\to\\pages.jsonl" : "https://example.test/page"}
            />
          </label>
          {importKind === "crawl" && (
            <label>
              <span>Max pages</span>
              <input
                type="number"
                min={1}
                max={100}
                value={crawlMaxPages}
                onChange={(event) => setCrawlMaxPages(Number(event.target.value))}
              />
            </label>
          )}
          <button type="submit" className="button primary" disabled={ingesting}>
            <Upload size={16} />
            Import
          </button>
        </form>
      </div>

      {jobResult && <JobResult result={jobResult} />}

      <div className="split-grid">
        <section className="panel">
          <PanelTitle icon={Database} title={`Sources (${sources.length})`} />
          <SourceList sources={sources} />
        </section>
        <section className="panel">
          <PanelTitle icon={FileText} title={`Documents (${documents.length})`} />
          <DocumentList documents={documents} />
        </section>
      </div>
    </section>
  );
}

function ArchivesView({ onRefreshStatus }: { onRefreshStatus: () => void | Promise<void> }) {
  const [files, setFiles] = useState<ArchiveFileRecord[]>([]);
  const [coverage, setCoverage] = useState<ArchiveCoverage | null>(null);
  const [loading, setLoading] = useState(false);
  const [importing, setImporting] = useState(false);
  const [counting, setCounting] = useState(false);
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState("");
  const [archivePath, setArchivePath] = useState("");
  const [archiveKind, setArchiveKind] = useState<"" | "post" | "comment">("");
  const [importResult, setImportResult] = useState<ArchiveImportResult | null>(null);
  const [countTerm, setCountTerm] = useState("");
  const [caseSensitive, setCaseSensitive] = useState(true);
  const [filterKind, setFilterKind] = useState<"" | "post" | "comment">("");
  const [filterSubreddit, setFilterSubreddit] = useState("");
  const [countResult, setCountResult] = useState<ArchiveCountResult | null>(null);
  const [searchQuery, setSearchQuery] = useState("");
  const [searchLimit, setSearchLimit] = useState(20);
  const [searchResults, setSearchResults] = useState<ArchiveSearchResult[]>([]);

  async function refreshArchives() {
    setLoading(true);
    setError("");
    try {
      const response = await fetchArchives();
      setFiles(response.files);
      setCoverage(response.coverage);
    } catch (listError) {
      setError(errorMessage(listError));
    } finally {
      setLoading(false);
    }
  }

  async function submitImport(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!archivePath.trim()) {
      setError("Archive import requires a local path.");
      return;
    }
    setImporting(true);
    setError("");
    try {
      const result = await importArchive({ path: archivePath.trim(), kind: archiveKind || undefined });
      setImportResult(result);
      await refreshArchives();
      await onRefreshStatus();
    } catch (importError) {
      setError(errorMessage(importError));
    } finally {
      setImporting(false);
    }
  }

  async function submitCount(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!countTerm.trim()) {
      setError("Archive count requires a term.");
      return;
    }
    setCounting(true);
    setError("");
    try {
      setCountResult(
        await countArchive({
          term: countTerm.trim(),
          case_sensitive: caseSensitive,
          kind: filterKind || undefined,
          subreddit: filterSubreddit.trim() || undefined,
        }),
      );
    } catch (countError) {
      setError(errorMessage(countError));
    } finally {
      setCounting(false);
    }
  }

  async function submitSearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSearching(true);
    setError("");
    try {
      const response = await searchArchive({
        query: searchQuery.trim(),
        limit: searchLimit,
        kind: filterKind || undefined,
        subreddit: filterSubreddit.trim() || undefined,
      });
      setSearchResults(response.results);
      setCoverage(response.coverage);
    } catch (searchError) {
      setError(errorMessage(searchError));
    } finally {
      setSearching(false);
    }
  }

  useEffect(() => {
    void refreshArchives();
  }, []);

  return (
    <section className="view">
      <ViewHeader
        eyebrow="Archives"
        title="Hybrid Reddit archive index"
        detail="Exact archive access, corpus coverage, and raw Reddit evidence for hybrid RAG."
        actions={
          <button type="button" className="button" onClick={refreshArchives} disabled={loading}>
            <RefreshCw size={16} className={loading ? "spin" : ""} />
            Refresh
          </button>
        }
      />

      {error && <InlineError message={error} />}

      <section className="panel">
        <PanelTitle icon={Archive} title="Archive coverage" />
        <ArchiveMetricGrid coverage={coverage} />
      </section>

      <div className="archive-grid">
        <form className="panel form-panel" onSubmit={submitImport}>
          <PanelTitle icon={Upload} title="Arctic Shift import" />
          <label>
            <span>Local archive path</span>
            <input
              value={archivePath}
              onChange={(event) => setArchivePath(event.target.value)}
              placeholder="C:\\path\\to\\r_subreddit_comments.jsonl"
            />
          </label>
          <label>
            <span>Fallback kind</span>
            <select value={archiveKind} onChange={(event) => setArchiveKind(event.target.value as "" | "post" | "comment")}>
              <option value="">auto</option>
              <option value="post">post</option>
              <option value="comment">comment</option>
            </select>
          </label>
          <button type="submit" className="button primary" disabled={importing}>
            {importing ? <Loader2 size={16} className="spin" /> : <Upload size={16} />}
            Import archive
          </button>
          {importResult && (
            <div className="inline-note">
              Indexed {importResult.indexed_count} rows from {importResult.file_format}; status {importResult.status}.
            </div>
          )}
        </form>

        <form className="panel form-panel" onSubmit={submitCount}>
          <PanelTitle icon={HashIcon} title="Exact count" />
          <label>
            <span>Term</span>
            <input value={countTerm} onChange={(event) => setCountTerm(event.target.value)} placeholder="P2P" />
          </label>
          <label className="toggle-row">
            <input type="checkbox" checked={caseSensitive} onChange={(event) => setCaseSensitive(event.target.checked)} />
            <span>Case-sensitive</span>
          </label>
          <ArchiveFilters
            kind={filterKind}
            subreddit={filterSubreddit}
            onKindChange={setFilterKind}
            onSubredditChange={setFilterSubreddit}
          />
          <button type="submit" className="button primary" disabled={counting}>
            {counting ? <Loader2 size={16} className="spin" /> : <Search size={16} />}
            Count
          </button>
          {countResult && <ArchiveCountSummary result={countResult} />}
        </form>

        <form className="panel form-panel" onSubmit={submitSearch}>
          <PanelTitle icon={Search} title="Archive search" />
          <label>
            <span>Query</span>
            <input value={searchQuery} onChange={(event) => setSearchQuery(event.target.value)} placeholder="needle" />
          </label>
          <label>
            <span>Limit</span>
            <input
              type="number"
              min={1}
              max={100}
              value={searchLimit}
              onChange={(event) => setSearchLimit(Number(event.target.value))}
            />
          </label>
          <ArchiveFilters
            kind={filterKind}
            subreddit={filterSubreddit}
            onKindChange={setFilterKind}
            onSubredditChange={setFilterSubreddit}
          />
          <button type="submit" className="button primary" disabled={searching}>
            {searching ? <Loader2 size={16} className="spin" /> : <Search size={16} />}
            Search archive
          </button>
        </form>
      </div>

      <div className="split-grid">
        <section className="panel">
          <PanelTitle icon={Archive} title={`Archive files (${files.length})`} />
          <ArchiveFileList files={files} />
        </section>
        <section className="panel">
          <PanelTitle icon={FileText} title={`Raw matches (${searchResults.length})`} />
          <ArchiveSearchList results={searchResults} />
        </section>
      </div>
    </section>
  );
}

function RetrievalLab() {
  const [query, setQuery] = useState("");
  const [topK, setTopK] = useState(8);
  const [result, setResult] = useState<RagSearchResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  async function submitSearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!query.trim()) {
      setError("Retrieval requires a query.");
      return;
    }
    setLoading(true);
    setError("");
    try {
      setResult(await searchRag({ query: query.trim(), top_k: topK, filters: {} }));
    } catch (searchError) {
      setError(errorMessage(searchError));
    } finally {
      setLoading(false);
    }
  }

  return (
    <section className="view">
      <ViewHeader
        eyebrow="Retrieval Lab"
        title="RAG query inspection"
        detail="Run retrieval directly and inspect packed context, citation IDs, metadata, scores, and chunks."
      />

      <form className="panel retrieval-form" onSubmit={submitSearch}>
        <label className="grow">
          <span>Query</span>
          <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search the corpus..." />
        </label>
        <label className="narrow-field">
          <span>top_k</span>
          <input
            type="number"
            min={1}
            max={40}
            value={topK}
            onChange={(event) => setTopK(Number(event.target.value))}
          />
        </label>
        <button type="submit" className="button primary" disabled={loading}>
          {loading ? <Loader2 size={16} className="spin" /> : <Search size={16} />}
          Search
        </button>
      </form>

      {error && <InlineError message={error} />}

      {!result ? (
        <EmptyState icon={Search} title="No retrieval run" detail="Submit a query to inspect the RAG packer output." />
      ) : (
        <div className="retrieval-grid">
          <section className="panel">
            <PanelTitle icon={FileText} title="Packed context" />
            <div className="meta-row">
              <span>Run ID</span>
              <code>{result.retrieval_run_id ?? "none"}</code>
            </div>
            <pre className="packed-context">{result.packed_context || "No packed context returned."}</pre>
          </section>
          <section className="panel">
            <PanelTitle icon={Search} title={`Results (${result.results.length})`} />
            <ResultList results={result.results} />
          </section>
        </div>
      )}
    </section>
  );
}

function StatusView(props: StatusPanelProps) {
  const { status, loading, error, captures, captureLoading, captureError, onRefresh, onCapture } = props;

  return (
    <section className="view">
      <ViewHeader
        eyebrow="Status"
        title="Runtime and model gate"
        detail="Backend reachability, model IDs, database counts, and screenshot capture readiness."
        actions={
          <button type="button" className="button" onClick={onRefresh} disabled={loading}>
            <RefreshCw size={16} className={loading ? "spin" : ""} />
            Refresh
          </button>
        }
      />
      {error && <InlineError message={error} />}
      <div className="status-main-grid">
        <section className="panel">
          <PanelTitle icon={Activity} title="Lemonade" />
          <KeyValue label="Reachable" value={<StatusPill ok={Boolean(status?.lemonade?.reachable)} />} />
          <KeyValue label="Base URL" value={<code>{status?.lemonade?.base_url ?? "unknown"}</code>} />
          {status?.lemonade?.error && <InlineError message={status.lemonade.error} />}
        </section>
        <section className="panel">
          <PanelTitle icon={Brain} title="Chat model" />
          <KeyValue label="ID" value={<code>{status?.model?.id ?? "unknown"}</code>} />
          <KeyValue label="Available" value={<StatusPill ok={Boolean(status?.model?.available)} />} />
          <KeyValue label="Context" value={formatValue(status?.model?.context_size)} />
          <KeyValue label="Backend" value={formatValue(status?.model?.backend)} />
          <KeyValue label="Args" value={<code>{status?.model?.args ?? "none"}</code>} />
        </section>
        <section className="panel">
          <PanelTitle icon={Wrench} title="Auxiliary models" />
          <KeyValue label="Embedding" value={<code>{status?.embedding?.id ?? "unknown"}</code>} />
          <KeyValue label="Reranker" value={<code>{status?.reranker?.id ?? "unknown"}</code>} />
          <KeyValue label="Classifier" value={<code>{status?.classifier?.id ?? "unknown"}</code>} />
        </section>
        <section className="panel">
          <PanelTitle icon={Database} title="Database counts" />
          <MetricGrid database={status?.database} />
        </section>
        <section className="panel">
          <PanelTitle icon={Archive} title="Archive coverage" />
          <ArchiveMetricGrid coverage={status?.archive} />
        </section>
        <section className="panel wide-panel">
          <PanelTitle icon={Camera} title="Vision and screenshots" />
          <VisionGate status={status} />
          <CaptureControls
            status={status}
            captures={captures}
            captureLoading={captureLoading}
            captureError={captureError}
            onCapture={onCapture}
          />
        </section>
      </div>
    </section>
  );
}

interface StatusPanelProps {
  status: StatusResponse | null;
  loading: boolean;
  error: string;
  captures: ScreenshotCapture[];
  captureLoading: boolean;
  captureError: string;
  onRefresh: () => void | Promise<void>;
  onCapture: () => void | Promise<void>;
}

function StatusInspector(props: StatusPanelProps) {
  const { status, loading, error, captures, captureLoading, captureError, onRefresh, onCapture } = props;

  return (
    <aside className="inspector">
      <div className="inspector-head">
        <div>
          <div className="label">Status</div>
          <h2>Runtime</h2>
        </div>
        <button type="button" className="icon-button" onClick={onRefresh} disabled={loading} title="Refresh status">
          <RefreshCw size={16} className={loading ? "spin" : ""} />
        </button>
      </div>
      {error && <InlineError message={error} />}
      <div className="status-stack">
        <KeyValue label="Lemonade" value={<StatusPill ok={Boolean(status?.lemonade?.reachable)} />} />
        <KeyValue label="Model" value={<code>{status?.model?.id ?? "unknown"}</code>} />
        <KeyValue label="Context" value={formatValue(status?.model?.context_size)} />
        <KeyValue label="Vision" value={<StatusPill ok={Boolean(status?.vision?.ready)} text={status?.vision?.reason} />} />
      </div>
      <MetricGrid database={status?.database} compact />
      <ArchiveMetricGrid coverage={status?.archive} compact />
      <CaptureControls
        status={status}
        captures={captures}
        captureLoading={captureLoading}
        captureError={captureError}
        onCapture={onCapture}
        compact
      />
    </aside>
  );
}

function CaptureControls(props: {
  status: StatusResponse | null;
  captures: ScreenshotCapture[];
  captureLoading: boolean;
  captureError: string;
  compact?: boolean;
  onCapture: () => void | Promise<void>;
}) {
  const { status, captures, captureLoading, captureError, compact, onCapture } = props;
  const visionReady = Boolean(status?.vision?.ready);
  const [submitNote, setSubmitNote] = useState("");

  function markForModelSubmission() {
    const latest = captures[0];
    if (!latest) {
      setSubmitNote("Capture a screenshot before model submission.");
      return;
    }
    setSubmitNote(`Attachment ${latest.attachment_id} is captured locally. The chat API does not expose an attachment field yet.`);
  }

  return (
    <div className={compact ? "capture compact" : "capture"}>
      <div className="capture-actions">
        <button type="button" className="button" onClick={onCapture} disabled={captureLoading}>
          {captureLoading ? <Loader2 size={16} className="spin" /> : <Camera size={16} />}
          Capture screenshot
        </button>
        <button
          type="button"
          className="button"
          disabled={!visionReady || captures.length === 0}
          onClick={markForModelSubmission}
          title={visionReady ? "Capture can be marked for a model call" : "Vision is blocked by the mmproj gate"}
        >
          <Send size={16} />
          Model submit
        </button>
      </div>
      {!visionReady && (
        <div className="muted small">Model submission is disabled until the status endpoint reports vision ready.</div>
      )}
      {captureError && <InlineError message={captureError} />}
      {submitNote && <div className="inline-note">{submitNote}</div>}
      <div className="capture-list">
        {captures.length === 0 ? (
          <div className="muted small">No captured attachments in this session.</div>
        ) : (
          captures.map((capture) => (
            <div className="capture-row" key={capture.attachment_id}>
              <div>
                <strong>#{capture.attachment_id}</strong>
                <span>{capture.width} x {capture.height}</span>
              </div>
              <code>{capture.path}</code>
              <span className="muted">{formatTime(capture.captured_at)}</span>
            </div>
          ))
        )}
      </div>
    </div>
  );
}

function MessageBubble({ message }: { message: UiMessage }) {
  return (
    <article className={`message ${message.role}`}>
      <div className="message-meta">
        <span>{message.role}</span>
        {message.mode && <code>{message.mode}</code>}
        {message.ragQuery && <span>RAG: {message.ragQuery}</span>}
        <span>{formatTime(message.createdAt)}</span>
        {message.status === "streaming" && <Loader2 size={14} className="spin" />}
      </div>
      {message.content && <div className="message-content">{message.content}</div>}
      {message.reasoning && (
        <details className="reasoning" open>
          <summary>
            <Brain size={14} />
            Reasoning
          </summary>
          <div>{message.reasoning}</div>
        </details>
      )}
      {message.events.length > 0 && (
        <div className="event-list">
          {message.events.map((event) => (
            <ChatEventBlock key={event.id} event={event} />
          ))}
        </div>
      )}
      {message.error && <InlineError message={message.error} />}
      {!message.content && message.status === "streaming" && (
        <div className="muted small">Waiting for first content token...</div>
      )}
    </article>
  );
}

function ChatEventBlock({ event }: { event: ChatEvent }) {
  if (event.event === "rag_context" && isRecord(event.data)) {
    const results = Array.isArray(event.data.results) ? (event.data.results as RagResult[]) : [];
    const packedContext = typeof event.data.packed_context === "string" ? event.data.packed_context : "";
    return (
      <details className="chat-event" open>
        <summary>
          <Search size={14} />
          Retrieved context ({results.length})
        </summary>
        <CitationRow results={results} />
        <pre>{packedContext || "No packed context."}</pre>
      </details>
    );
  }

  if (event.event === "archive_context" && isRecord(event.data)) {
    const results = Array.isArray(event.data.results) ? (event.data.results as ArchiveSearchResult[]) : [];
    const packedContext = typeof event.data.packed_context === "string" ? event.data.packed_context : "";
    return (
      <details className="chat-event" open>
        <summary>
          <Archive size={14} />
          Archive context ({results.length})
        </summary>
        <pre>{packedContext || "No archive context."}</pre>
      </details>
    );
  }

  if (event.event === "timing") {
    return (
      <details className="chat-event" open>
        <summary>
          <Clock3 size={14} />
          Timing
        </summary>
        <JsonBlock value={event.data} />
      </details>
    );
  }

  if (event.event === "tool_call") {
    return (
      <details className="chat-event" open>
        <summary>
          <Wrench size={14} />
          Tool call
        </summary>
        <JsonBlock value={event.data} />
      </details>
    );
  }

  return (
    <details className="chat-event">
      <summary>{event.event}</summary>
      <JsonBlock value={event.data} />
    </details>
  );
}

function ArchiveFilters(props: {
  kind: "" | "post" | "comment";
  subreddit: string;
  onKindChange: (value: "" | "post" | "comment") => void;
  onSubredditChange: (value: string) => void;
}) {
  return (
    <div className="filter-row">
      <label>
        <span>Kind</span>
        <select value={props.kind} onChange={(event) => props.onKindChange(event.target.value as "" | "post" | "comment")}>
          <option value="">all</option>
          <option value="post">posts</option>
          <option value="comment">comments</option>
        </select>
      </label>
      <label>
        <span>Subreddit</span>
        <input value={props.subreddit} onChange={(event) => props.onSubredditChange(event.target.value)} placeholder="theehive" />
      </label>
    </div>
  );
}

function ArchiveCountSummary({ result }: { result: ArchiveCountResult }) {
  const byKind = Object.entries(result.by_kind)
    .map(([kind, count]) => `${kind}: ${count}`)
    .join(", ");
  return (
    <div className="archive-count-summary">
      <Metric label="Occurrences" value={String(result.occurrences)} />
      <Metric label="Matched rows" value={String(result.matched_items)} />
      <Metric label="Searched rows" value={String(result.searched_items)} />
      <div className="muted small">{byKind || "No matching archive rows."}</div>
    </div>
  );
}

function ArchiveFileList({ files }: { files: ArchiveFileRecord[] }) {
  if (files.length === 0) {
    return <EmptyState icon={Archive} title="No archive files" detail="Import an Arctic Shift archive to populate this list." compact />;
  }
  return (
    <div className="record-list">
      {files.map((file) => (
        <div className="record-row" key={file.id}>
          <div className="record-main">
            <strong>{file.path}</strong>
            <span>{file.log || `${file.indexed_count} indexed, ${file.failed_count} failed`}</span>
          </div>
          <div className="record-meta">
            <code>{file.file_format}</code>
            <code>{file.fallback_kind || "auto"}</code>
            <StatusPill ok={file.status === "completed"} text={file.status} />
            <span>{formatTime(file.updated_at)}</span>
          </div>
        </div>
      ))}
    </div>
  );
}

function ArchiveSearchList({ results }: { results: ArchiveSearchResult[] }) {
  if (results.length === 0) {
    return <EmptyState icon={Search} title="No raw matches" detail="Run an archive search to inspect Reddit rows." compact />;
  }
  return (
    <div className="result-list">
      {results.map((result) => (
        <article className="result-row" key={result.id}>
          <div className="result-head">
            <code>R{result.id}</code>
            <span>
              {result.kind} r/{result.subreddit || "unknown"} u/{result.author || "unknown"}
            </span>
          </div>
          <div className="metadata-strip">
            <span>{result.score === null || result.score === undefined ? "score unknown" : `score ${result.score}`}</span>
            <span>{result.created_utc ? new Date(result.created_utc * 1000).toLocaleString() : "date unknown"}</span>
            <span>{result.permalink || result.url || "no link"}</span>
          </div>
          <p>{result.text}</p>
        </article>
      ))}
    </div>
  );
}

function SourceList({ sources }: { sources: SourceRecord[] }) {
  if (sources.length === 0) {
    return <EmptyState icon={Database} title="No sources" detail="Ingest content to populate the source table." compact />;
  }
  return (
    <div className="record-list">
      {sources.map((source) => (
        <div className="record-row" key={source.id}>
          <div className="record-main">
            <strong>{source.title}</strong>
            <span>{source.uri}</span>
          </div>
          <div className="record-meta">
            <code>{source.source_type}</code>
            <StatusPill ok={source.status === "ready"} text={source.status} />
            <span>{formatTime(source.updated_at)}</span>
          </div>
        </div>
      ))}
    </div>
  );
}

function DocumentList({ documents }: { documents: DocumentRecord[] }) {
  if (documents.length === 0) {
    return <EmptyState icon={FileText} title="No documents" detail="Documents are created during ingestion." compact />;
  }
  return (
    <div className="record-list">
      {documents.map((document) => (
        <div className="record-row" key={document.id}>
          <div className="record-main">
            <strong>{document.title}</strong>
            <span>{document.summary || "No summary"}</span>
          </div>
          <div className="record-meta">
            <code>{document.document_type ?? "unknown"}</code>
            <code>{document.source_type ?? "unknown"}</code>
            <span>source {document.source_id}</span>
          </div>
        </div>
      ))}
    </div>
  );
}

function ResultList({ results }: { results: RagResult[] }) {
  if (results.length === 0) {
    return <EmptyState icon={Search} title="No results" detail="The retriever returned no chunks." compact />;
  }
  return (
    <div className="result-list">
      {results.map((result) => (
        <article className="result-row" key={`${result.chunk_id}-${result.citation_id}`}>
          <div className="result-head">
            <code>{result.citation_id}</code>
            <span>score {formatScore(result.score)}</span>
          </div>
          <div className="metadata-strip">
            <span>{metadataText(result.metadata, "title")}</span>
            <span>{metadataText(result.metadata, "document_type")}</span>
            <span>{metadataList(result.metadata, "tags")}</span>
          </div>
          <p>{result.text}</p>
        </article>
      ))}
    </div>
  );
}

function JobResult({ result }: { result: IngestionResult }) {
  return (
    <section className="panel job-result">
      <PanelTitle icon={CheckCircle2} title={`Job ${result.job_id}: ${result.status}`} />
      <div className="metric-row">
        <Metric label="Processed" value={String(result.processed_count)} />
        <Metric label="Failed" value={String(result.failed_count)} />
      </div>
      {result.log && <pre className="job-log">{result.log}</pre>}
    </section>
  );
}

function MetricGrid({ database, compact }: { database?: StatusResponse["database"]; compact?: boolean }) {
  return (
    <div className={compact ? "metric-grid compact" : "metric-grid"}>
      <Metric label="Sources" value={formatValue(database?.sources)} />
      <Metric label="Docs" value={formatValue(database?.documents)} />
      <Metric label="Chunks" value={formatValue(database?.chunks)} />
      <Metric label="Embeds" value={formatValue(database?.embeddings)} />
    </div>
  );
}

function ArchiveMetricGrid({ coverage, compact }: { coverage?: ArchiveCoverage | null; compact?: boolean }) {
  return (
    <div className={compact ? "metric-grid compact" : "metric-grid"}>
      <Metric label="Files" value={formatValue(coverage?.files)} />
      <Metric label="Items" value={formatValue(coverage?.items)} />
      <Metric label="Posts" value={formatValue(coverage?.posts)} />
      <Metric label="Comments" value={formatValue(coverage?.comments)} />
      <Metric label="Semantic chunks" value={formatValue(coverage?.semantic_chunks)} />
      <Metric label="Embedded rows" value={formatValue(coverage?.embedded_items)} />
    </div>
  );
}

function VisionGate({ status }: { status: StatusResponse | null }) {
  const ready = Boolean(status?.vision?.ready);
  return (
    <div className={`vision-gate ${ready ? "ready" : "blocked"}`}>
      {ready ? <CheckCircle2 size={17} /> : <CircleAlert size={17} />}
      <div>
        <strong>{ready ? "Vision ready" : "Vision gated"}</strong>
        <span>{status?.vision?.message || status?.vision?.reason || "No vision status returned."}</span>
      </div>
    </div>
  );
}

function ViewHeader(props: { eyebrow: string; title: string; detail: string; actions?: React.ReactNode }) {
  return (
    <header className="view-header">
      <div>
        <div className="label">{props.eyebrow}</div>
        <h1>{props.title}</h1>
        <p>{props.detail}</p>
      </div>
      {props.actions && <div className="view-actions">{props.actions}</div>}
    </header>
  );
}

function PanelTitle({ icon: Icon, title }: { icon: LucideIcon; title: string }) {
  return (
    <div className="panel-title">
      <Icon size={16} />
      <h2>{title}</h2>
    </div>
  );
}

function KeyValue({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="kv">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function Metric({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="metric">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function StatusPill({ ok, text }: { ok: boolean; text?: string }) {
  return (
    <span className={`pill ${ok ? "ok" : "bad"}`}>
      {ok ? <CheckCircle2 size={13} /> : <XCircle size={13} />}
      {text || (ok ? "ok" : "blocked")}
    </span>
  );
}

function InlineError({ message }: { message: string }) {
  return (
    <div className="inline-error">
      <CircleAlert size={16} />
      <span>{message}</span>
    </div>
  );
}

function EmptyState(props: { icon: LucideIcon; title: string; detail: string; compact?: boolean }) {
  const Icon = props.icon;
  return (
    <div className={props.compact ? "empty compact" : "empty"}>
      <Icon size={22} />
      <strong>{props.title}</strong>
      <span>{props.detail}</span>
    </div>
  );
}

function CitationRow({ results }: { results: RagResult[] }) {
  if (results.length === 0) {
    return <div className="muted small">No citations returned.</div>;
  }
  return (
    <div className="citation-row">
      {results.map((result) => (
        <span key={`${result.chunk_id}-${result.citation_id}`}>
          {result.citation_id} <em>{formatScore(result.score)}</em>
        </span>
      ))}
    </div>
  );
}

function JsonBlock({ value }: { value: unknown }) {
  return <pre className="json-block">{JSON.stringify(value, null, 2)}</pre>;
}

function applyChatFrame(
  assistantId: string,
  frame: SseFrame,
  setMessages: React.Dispatch<React.SetStateAction<UiMessage[]>>,
) {
  setMessages((current) =>
    current.map((message) => {
      if (message.id !== assistantId) {
        return message;
      }
      if (frame.event === "reasoning") {
        return { ...message, reasoning: message.reasoning + textFromFrame(frame) };
      }
      if (frame.event === "content") {
        return { ...message, content: message.content + textFromFrame(frame) };
      }
      if (frame.event === "done") {
        return { ...message, status: "done" };
      }
      return {
        ...message,
        events: [...message.events, { id: makeId("evt"), event: frame.event, data: frame.data }],
      };
    }),
  );
}

function messageFromRecord(message: ConversationMessage): UiMessage {
  return {
    id: `saved-${message.id}`,
    role: message.role === "assistant" ? "assistant" : "user",
    content: message.content,
    reasoning: message.reasoning || "",
    events: [],
    status: "done",
    createdAt: message.created_at || new Date().toISOString(),
  };
}

function textFromFrame(frame: SseFrame): string {
  if (isRecord(frame.data) && typeof frame.data.text === "string") {
    return frame.data.text;
  }
  return "";
}

function metadataText(metadata: JsonRecord | undefined, key: string): string {
  const value = metadata?.[key];
  return typeof value === "string" && value.trim() ? value : "unknown";
}

function metadataList(metadata: JsonRecord | undefined, key: string): string {
  const value = metadata?.[key];
  if (Array.isArray(value) && value.length > 0) {
    return value.map(String).slice(0, 4).join(", ");
  }
  return "no tags";
}

function formatScore(score: number): string {
  if (!Number.isFinite(score)) {
    return "0.000";
  }
  return score.toFixed(3);
}

function formatValue(value: unknown): string {
  if (value === null || value === undefined || value === "") {
    return "unknown";
  }
  return String(value);
}

function formatModelOption(model: ModelOption): string {
  const context = model.context_size ? ` (${model.context_size} ctx)` : "";
  return `${model.id}${context}`;
}

function formatTime(value: string | undefined): string {
  if (!value) {
    return "unknown";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleString();
}

function makeId(prefix: string): string {
  return `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function errorMessage(error: unknown): string {
  if (error instanceof Error) {
    return error.message;
  }
  return String(error);
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

function isRecord(value: unknown): value is JsonRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export default App;
