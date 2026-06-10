import { useEffect, useRef, useState } from "react";
import {
  KBEntry,
  KBStats,
  addKnowledgeEntry,
  deleteKnowledgeEntry,
  getKBStats,
  getKnowledgeBase,
  toggleKnowledgeEntry,
  updateKnowledgeEntry,
  uploadKnowledgeFile,
} from "../api/client";
import Layout from "../components/Layout";
import {
  BookOpen,
  Brain,
  CheckCircle2,
  Edit2,
  FileText,
  Plus,
  Search,
  Trash2,
  Upload,
  X,
} from "lucide-react";

// ── Helpers ───────────────────────────────────────────────────────────────────

const CATEGORIES = ["delivery", "product", "payment", "policy", "general"];
const LANGUAGES  = ["english", "hindi", "gujarati", "hinglish"];

function timeAgo(iso: string | null): string {
  if (!iso) return "—";
  const diff = Date.now() - new Date(iso).getTime();
  const mins  = Math.floor(diff / 60000);
  const hours = Math.floor(mins / 60);
  const days  = Math.floor(hours / 24);
  if (days > 0)  return `${days}d ago`;
  if (hours > 0) return `${hours}h ago`;
  if (mins > 0)  return `${mins}m ago`;
  return "just now";
}

function SourceBadge({ source }: { source: string }) {
  const map: Record<string, string> = {
    auto_learned: "bg-blue-100 text-blue-700",
    manual:       "bg-green-100 text-green-700",
    pdf_upload:   "bg-purple-100 text-purple-700",
  };
  const label: Record<string, string> = {
    auto_learned: "Auto-learned",
    manual:       "Manual",
    pdf_upload:   "PDF",
  };
  const cls = map[source] ?? "bg-gray-100 text-gray-600";
  return (
    <span className={`inline-block px-2 py-0.5 rounded-full text-xs font-medium ${cls}`}>
      {label[source] ?? source}
    </span>
  );
}

function CategoryBadge({ category }: { category: string | null }) {
  if (!category) return null;
  return (
    <span className="inline-block px-2 py-0.5 rounded-full text-xs font-medium bg-gray-100 text-gray-600 capitalize">
      {category}
    </span>
  );
}

// ── Add / Edit Modal ──────────────────────────────────────────────────────────

interface EntryModalProps {
  initial?: KBEntry | null;
  onClose: () => void;
  onSaved: (entry: KBEntry) => void;
}

function EntryModal({ initial, onClose, onSaved }: EntryModalProps) {
  const [question, setQuestion] = useState(initial?.question ?? "");
  const [answer,   setAnswer]   = useState(initial?.answer   ?? "");
  const [category, setCategory] = useState(initial?.category ?? "");
  const [language, setLanguage] = useState(initial?.language ?? "english");
  const [saving,   setSaving]   = useState(false);
  const [error,    setError]    = useState("");

  async function handleSave() {
    if (!question.trim() || !answer.trim()) {
      setError("Question and answer are required.");
      return;
    }
    setSaving(true);
    setError("");
    try {
      let saved: KBEntry;
      if (initial) {
        saved = await updateKnowledgeEntry(initial.id, {
          question: question.trim(),
          answer:   answer.trim(),
          category: category || undefined,
          language,
        });
      } else {
        saved = await addKnowledgeEntry({
          question: question.trim(),
          answer:   answer.trim(),
          category: category || undefined,
          language,
        });
      }
      onSaved(saved);
    } catch {
      setError("Failed to save entry. Please try again.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
      <div className="bg-white rounded-2xl shadow-xl w-full max-w-lg">
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-100">
          <h2 className="font-semibold text-gray-900">
            {initial ? "Edit Entry" : "Add Knowledge Entry"}
          </h2>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600">
            <X size={20} />
          </button>
        </div>

        <div className="px-6 py-5 space-y-4">
          {error && (
            <p className="text-sm text-red-600 bg-red-50 rounded-lg px-3 py-2">{error}</p>
          )}

          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">Question / Keywords</label>
            <input
              className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-400"
              placeholder="e.g. delivery time kitna lagta hai"
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
            />
            <p className="text-xs text-gray-400 mt-1">
              Keywords help the agent find this answer. Include common variations.
            </p>
          </div>

          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">Answer</label>
            <textarea
              rows={4}
              className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-400 resize-none"
              placeholder="e.g. We deliver pan-India in 3-5 business days..."
              value={answer}
              onChange={(e) => setAnswer(e.target.value)}
            />
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">Category</label>
              <select
                className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-400"
                value={category}
                onChange={(e) => setCategory(e.target.value)}
              >
                <option value="">— None —</option>
                {CATEGORIES.map((c) => (
                  <option key={c} value={c} className="capitalize">{c}</option>
                ))}
              </select>
            </div>
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">Language</label>
              <select
                className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-400"
                value={language}
                onChange={(e) => setLanguage(e.target.value)}
              >
                {LANGUAGES.map((l) => (
                  <option key={l} value={l} className="capitalize">{l}</option>
                ))}
              </select>
            </div>
          </div>
        </div>

        <div className="flex justify-end gap-3 px-6 py-4 border-t border-gray-100">
          <button
            onClick={onClose}
            className="px-4 py-2 text-sm text-gray-600 hover:text-gray-900"
          >
            Cancel
          </button>
          <button
            onClick={handleSave}
            disabled={saving}
            className="px-5 py-2 bg-indigo-600 text-white text-sm rounded-lg hover:bg-indigo-700 disabled:opacity-50 transition-colors"
          >
            {saving ? "Saving…" : "Save Entry"}
          </button>
        </div>
      </div>
    </div>
  );
}

// ── PDF Upload Section ────────────────────────────────────────────────────────

function UploadSection({ onUploaded }: { onUploaded: () => void }) {
  const inputRef  = useRef<HTMLInputElement>(null);
  const [file,    setFile]    = useState<File | null>(null);
  const [status,  setStatus]  = useState<"idle" | "uploading" | "done" | "error">("idle");
  const [result,  setResult]  = useState<{ entries_added: number; filename: string } | null>(null);
  const [errMsg,  setErrMsg]  = useState("");

  function handleDrop(e: React.DragEvent) {
    e.preventDefault();
    const dropped = e.dataTransfer.files[0];
    if (dropped) setFile(dropped);
  }

  async function handleUpload() {
    if (!file) return;
    setStatus("uploading");
    setErrMsg("");
    try {
      const res = await uploadKnowledgeFile(file);
      setResult(res);
      setStatus("done");
      onUploaded();
    } catch {
      setStatus("error");
      setErrMsg("Upload failed. Make sure the file is PDF, DOC, or TXT.");
    }
  }

  return (
    <div className="bg-white rounded-2xl border border-gray-200 p-6">
      <div className="flex items-center gap-2 mb-4">
        <FileText size={18} className="text-purple-600" />
        <h3 className="font-semibold text-gray-900 text-sm">Upload Documents</h3>
        <span className="text-xs text-gray-400">Agent learns from your files</span>
      </div>

      <div
        onDragOver={(e) => e.preventDefault()}
        onDrop={handleDrop}
        onClick={() => inputRef.current?.click()}
        className="border-2 border-dashed border-gray-200 rounded-xl p-8 text-center cursor-pointer hover:border-indigo-300 hover:bg-indigo-50/30 transition-colors"
      >
        <Upload size={24} className="mx-auto text-gray-300 mb-2" />
        <p className="text-sm text-gray-500">
          {file ? file.name : "Drop a PDF, DOC, or TXT file here, or click to browse"}
        </p>
        {file && (
          <p className="text-xs text-gray-400 mt-1">
            {(file.size / 1024).toFixed(1)} KB
          </p>
        )}
        <input
          ref={inputRef}
          type="file"
          accept=".pdf,.doc,.docx,.txt"
          className="hidden"
          onChange={(e) => e.target.files?.[0] && setFile(e.target.files[0])}
        />
      </div>

      {status === "error" && (
        <p className="text-xs text-red-600 mt-2">{errMsg}</p>
      )}

      {status === "done" && result && (
        <div className="mt-3 flex items-center gap-2 text-sm text-green-700 bg-green-50 rounded-lg px-3 py-2">
          <CheckCircle2 size={16} />
          {result.entries_added} entries learned from {result.filename}
        </div>
      )}

      {file && status !== "done" && (
        <div className="mt-3 flex justify-end">
          <button
            onClick={handleUpload}
            disabled={status === "uploading"}
            className="px-4 py-2 bg-purple-600 text-white text-sm rounded-lg hover:bg-purple-700 disabled:opacity-50 transition-colors flex items-center gap-2"
          >
            <Upload size={14} />
            {status === "uploading" ? "Processing…" : "Process File"}
          </button>
        </div>
      )}
    </div>
  );
}

// ── Entry Card ────────────────────────────────────────────────────────────────

interface EntryCardProps {
  entry: KBEntry;
  onEdit:   (e: KBEntry) => void;
  onDelete: (id: number) => void;
  onToggle: (id: number) => void;
}

function EntryCard({ entry, onEdit, onDelete, onToggle }: EntryCardProps) {
  return (
    <div className={`bg-white rounded-xl border p-4 transition-opacity ${entry.is_active ? "border-gray-200" : "border-gray-100 opacity-60"}`}>
      <div className="flex items-start justify-between gap-3 mb-2">
        <div className="flex items-center gap-2 flex-wrap">
          <CategoryBadge category={entry.category} />
          <SourceBadge source={entry.source} />
        </div>

        {/* Toggle switch */}
        <button
          onClick={() => onToggle(entry.id)}
          title={entry.is_active ? "Disable entry" : "Enable entry"}
          className={`relative inline-flex h-5 w-9 shrink-0 rounded-full border-2 border-transparent transition-colors ${
            entry.is_active ? "bg-indigo-600" : "bg-gray-200"
          }`}
        >
          <span
            className={`pointer-events-none inline-block h-4 w-4 rounded-full bg-white shadow transform transition-transform ${
              entry.is_active ? "translate-x-4" : "translate-x-0"
            }`}
          />
        </button>
      </div>

      <p className="text-sm font-medium text-gray-800 mb-1 leading-snug">
        Q: {entry.question}
      </p>
      <p className="text-sm text-gray-500 leading-snug line-clamp-2">
        A: {entry.answer}
      </p>

      <div className="flex items-center justify-between mt-3">
        <div className="flex items-center gap-3 text-xs text-gray-400">
          <span>Used {entry.usage_count}×</span>
          <span>·</span>
          <span>Added {timeAgo(entry.created_at)}</span>
        </div>
        <div className="flex items-center gap-1">
          <button
            onClick={() => onEdit(entry)}
            className="p-1.5 text-gray-400 hover:text-indigo-600 hover:bg-indigo-50 rounded-lg transition-colors"
            title="Edit"
          >
            <Edit2 size={14} />
          </button>
          <button
            onClick={() => onDelete(entry.id)}
            className="p-1.5 text-gray-400 hover:text-red-500 hover:bg-red-50 rounded-lg transition-colors"
            title="Delete"
          >
            <Trash2 size={14} />
          </button>
        </div>
      </div>
    </div>
  );
}

// ── Main Page ─────────────────────────────────────────────────────────────────

export default function KnowledgeBase() {
  const [entries,     setEntries]     = useState<KBEntry[]>([]);
  const [stats,       setStats]       = useState<KBStats | null>(null);
  const [loading,     setLoading]     = useState(true);
  const [search,      setSearch]      = useState("");
  const [filterSrc,   setFilterSrc]   = useState("all");
  const [filterCat,   setFilterCat]   = useState("all");
  const [modalEntry,  setModalEntry]  = useState<KBEntry | null | undefined>(undefined);
  // undefined = closed, null = new, KBEntry = editing

  async function loadAll() {
    setLoading(true);
    try {
      const [list, s] = await Promise.all([getKnowledgeBase(), getKBStats()]);
      setEntries(list);
      setStats(s);
    } catch {
      // silently fail — entries stay empty
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { loadAll(); }, []);

  async function handleToggle(id: number) {
    try {
      const updated = await toggleKnowledgeEntry(id);
      setEntries((prev) => prev.map((e) => (e.id === id ? updated : e)));
    } catch { /* ignore */ }
  }

  async function handleDelete(id: number) {
    if (!confirm("Delete this knowledge entry? The agent will stop using it immediately.")) return;
    try {
      await deleteKnowledgeEntry(id);
      setEntries((prev) => prev.filter((e) => e.id !== id));
      setStats((s) => s ? { ...s, total: s.total - 1 } : s);
    } catch { /* ignore */ }
  }

  function handleSaved(entry: KBEntry) {
    setEntries((prev) => {
      const idx = prev.findIndex((e) => e.id === entry.id);
      if (idx >= 0) {
        const next = [...prev];
        next[idx] = entry;
        return next;
      }
      return [entry, ...prev];
    });
    setModalEntry(undefined);
    loadAll(); // refresh stats
  }

  // Filter + search
  const visible = entries.filter((e) => {
    if (filterSrc !== "all" && e.source !== filterSrc) return false;
    if (filterCat !== "all" && (e.category ?? "general") !== filterCat) return false;
    if (search) {
      const q = search.toLowerCase();
      return e.question.toLowerCase().includes(q) || e.answer.toLowerCase().includes(q);
    }
    return true;
  });

  return (
    <Layout>
      <div className="max-w-5xl mx-auto space-y-6">
        {/* Header */}
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-bold text-gray-900 flex items-center gap-2">
              <Brain size={24} className="text-indigo-600" />
              Knowledge Base
            </h1>
            <p className="text-sm text-gray-500 mt-0.5">
              Your agent learns from this data · Auto-updates every Sunday night
            </p>
          </div>
          <button
            onClick={() => setModalEntry(null)}
            className="flex items-center gap-2 px-4 py-2 bg-indigo-600 text-white text-sm rounded-xl hover:bg-indigo-700 transition-colors shadow-sm"
          >
            <Plus size={16} />
            Add Entry
          </button>
        </div>

        {/* Stats row */}
        {stats && (
          <div className="grid grid-cols-3 gap-4">
            <div className="bg-white rounded-2xl border border-gray-200 px-5 py-4 flex items-center gap-4">
              <div className="w-10 h-10 bg-indigo-50 rounded-xl flex items-center justify-center">
                <BookOpen size={20} className="text-indigo-600" />
              </div>
              <div>
                <p className="text-2xl font-bold text-gray-900">{stats.total}</p>
                <p className="text-xs text-gray-500">Total Entries</p>
              </div>
            </div>
            <div className="bg-white rounded-2xl border border-gray-200 px-5 py-4 flex items-center gap-4">
              <div className="w-10 h-10 bg-blue-50 rounded-xl flex items-center justify-center">
                <Brain size={20} className="text-blue-600" />
              </div>
              <div>
                <p className="text-2xl font-bold text-gray-900">{stats.auto_learned_count}</p>
                <p className="text-xs text-gray-500">Auto-Learned</p>
              </div>
            </div>
            <div className="bg-white rounded-2xl border border-gray-200 px-5 py-4 flex items-center gap-4">
              <div className="w-10 h-10 bg-green-50 rounded-xl flex items-center justify-center">
                <CheckCircle2 size={20} className="text-green-600" />
              </div>
              <div>
                <p className="text-2xl font-bold text-gray-900">{stats.manual_count}</p>
                <p className="text-xs text-gray-500">Manual</p>
              </div>
            </div>
          </div>
        )}

        {/* Filter bar */}
        <div className="bg-white rounded-2xl border border-gray-200 px-4 py-3 flex flex-wrap items-center gap-3">
          {/* Source filter */}
          <div className="flex items-center gap-1 text-xs">
            <span className="text-gray-400 font-medium mr-1">Source:</span>
            {(["all", "auto_learned", "manual", "pdf_upload"] as const).map((s) => (
              <button
                key={s}
                onClick={() => setFilterSrc(s)}
                className={`px-2.5 py-1 rounded-lg font-medium transition-colors ${
                  filterSrc === s
                    ? "bg-indigo-600 text-white"
                    : "text-gray-500 hover:bg-gray-100"
                }`}
              >
                {s === "all" ? "All" : s === "auto_learned" ? "Auto-learned" : s === "pdf_upload" ? "PDF" : "Manual"}
              </button>
            ))}
          </div>

          <div className="w-px h-4 bg-gray-200" />

          {/* Category filter */}
          <div className="flex items-center gap-1 text-xs">
            <span className="text-gray-400 font-medium mr-1">Category:</span>
            {(["all", ...CATEGORIES] as const).map((c) => (
              <button
                key={c}
                onClick={() => setFilterCat(c)}
                className={`px-2.5 py-1 rounded-lg font-medium capitalize transition-colors ${
                  filterCat === c
                    ? "bg-indigo-600 text-white"
                    : "text-gray-500 hover:bg-gray-100"
                }`}
              >
                {c === "all" ? "All" : c}
              </button>
            ))}
          </div>

          <div className="ml-auto flex items-center gap-2 border border-gray-200 rounded-lg px-3 py-1.5">
            <Search size={14} className="text-gray-400" />
            <input
              type="text"
              placeholder="Search entries…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="text-sm focus:outline-none w-44 placeholder-gray-400"
            />
          </div>
        </div>

        {/* PDF upload */}
        <UploadSection onUploaded={loadAll} />

        {/* Entries list */}
        {loading ? (
          <div className="text-center py-16 text-gray-400 text-sm">Loading…</div>
        ) : visible.length === 0 ? (
          <div className="text-center py-16">
            <BookOpen size={36} className="mx-auto text-gray-200 mb-3" />
            <p className="text-gray-500 text-sm">
              {entries.length === 0
                ? "No entries yet. Add your first one or run seed.py."
                : "No entries match your filters."}
            </p>
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {visible.map((entry) => (
              <EntryCard
                key={entry.id}
                entry={entry}
                onEdit={(e) => setModalEntry(e)}
                onDelete={handleDelete}
                onToggle={handleToggle}
              />
            ))}
          </div>
        )}
      </div>

      {/* Add / Edit modal */}
      {modalEntry !== undefined && (
        <EntryModal
          initial={modalEntry}
          onClose={() => setModalEntry(undefined)}
          onSaved={handleSaved}
        />
      )}
    </Layout>
  );
}
