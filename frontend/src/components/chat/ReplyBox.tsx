import { useEffect, useMemo, useRef, useState } from "react";
import { Clock, Paperclip, Send, X } from "lucide-react";
import { useTranslation } from "react-i18next";
import type { ApprovedTemplate, ChatDetail } from "../../types/chat";
import { durationShort } from "../../utils/time";

interface Props {
  detail: ChatDetail;
  templates: ApprovedTemplate[];
  templatesLoading: boolean;
  onSendText: (text: string) => void;
  onSendImage: (file: File, caption: string) => void;
  onSendTemplate: (tpl: ApprovedTemplate, variables: string[]) => void;
}

const MAX_IMAGE_BYTES = 5 * 1024 * 1024;

/** Live "seconds left" for the 24h window; re-derived from the server's closes_at every 30 s. */
function useWindowSecondsLeft(closesAt: string | null): number {
  const calc = () => (closesAt ? Math.max(0, Math.floor((new Date(closesAt).getTime() - Date.now()) / 1000)) : 0);
  const [left, setLeft] = useState(calc);
  useEffect(() => {
    setLeft(calc());
    const id = window.setInterval(() => setLeft(calc()), 30_000);
    return () => window.clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [closesAt]);
  return left;
}

function placeholdersOf(body: string): number {
  const nums = [...body.matchAll(/\{\{(\d+)\}\}/g)].map((m) => Number(m[1]));
  return nums.length ? Math.max(...nums) : 0;
}

/** Template chooser shown instead of free text once Meta's 24h window has closed. */
function TemplatePicker({
  templates, loading, onSend,
}: { templates: ApprovedTemplate[]; loading: boolean; onSend: (tpl: ApprovedTemplate, vars: string[]) => void }) {
  const { t } = useTranslation();
  const [selected, setSelected] = useState<ApprovedTemplate | null>(null);
  const [vars, setVars] = useState<string[]>([]);

  function pick(tpl: ApprovedTemplate) {
    setSelected(tpl);
    setVars(Array(placeholdersOf(tpl.body)).fill(""));
  }

  if (loading) return <div className="h-10 rounded-lg bg-gray-100 animate-pulse" />;
  if (templates.length === 0) {
    return <p className="text-xs text-gray-500">{t("chat.no_templates")}</p>;
  }
  return (
    <div className="space-y-2">
      <div className="flex gap-2 overflow-x-auto scrollbar-hide pb-1">
        {templates.map((tpl) => (
          <button
            key={tpl.id}
            onClick={() => pick(tpl)}
            className={`shrink-0 text-xs px-3 py-1.5 rounded-full border transition-colors ${
              selected?.id === tpl.id
                ? "bg-brand-secondary text-white border-brand-secondary"
                : "border-gray-200 text-gray-700 hover:border-brand-secondary/40"
            }`}
          >
            {tpl.name}
          </button>
        ))}
      </div>
      {selected && (
        <div className="rounded-xl border border-gray-200 bg-brand-bg p-3 space-y-2">
          <p className="text-xs text-gray-600 whitespace-pre-wrap">{selected.body}</p>
          {vars.map((v, i) => (
            <input
              key={i}
              value={v}
              onChange={(e) => setVars((prev) => prev.map((x, j) => (j === i ? e.target.value : x)))}
              placeholder={t("chat.template_var", { n: i + 1 })}
              className="w-full border border-gray-200 rounded-lg px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-brand-primary"
            />
          ))}
          <button
            disabled={vars.some((v) => !v.trim())}
            onClick={() => onSend(selected, vars.map((v) => v.trim()))}
            className="w-full bg-brand-primary text-white text-sm font-semibold py-2 rounded-lg hover:bg-brand-primaryDark disabled:opacity-50 transition-colors"
          >
            {t("chat.send_template")}
          </button>
        </div>
      )}
    </div>
  );
}

/** Reply composer: text (Enter sends, Shift+Enter newline), image attach, 24h-window timer and template fallback. */
export default function ReplyBox({ detail, templates, templatesLoading, onSendText, onSendImage, onSendTemplate }: Props) {
  const { t } = useTranslation();
  const [text, setText] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [fileError, setFileError] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const area = useRef<HTMLTextAreaElement>(null);

  const secondsLeft = useWindowSecondsLeft(detail.window.closes_at);
  const windowOpen = detail.window.open && secondsLeft > 0;
  const preview = useMemo(() => (file ? URL.createObjectURL(file) : null), [file]);
  useEffect(() => () => { if (preview) URL.revokeObjectURL(preview); }, [preview]);

  // Reset the composer when switching conversations.
  useEffect(() => {
    setText("");
    setFile(null);
    setFileError(null);
  }, [detail.id]);

  const blocked = detail.opted_out;
  const canType = windowOpen && !blocked;

  function submit() {
    if (!canType) return;
    const body = text.trim();
    if (file) {
      onSendImage(file, body);
    } else if (body) {
      onSendText(body);
    } else {
      return;
    }
    setText("");
    setFile(null);
    if (area.current) area.current.style.height = "auto";
  }

  function onPick(e: React.ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0];
    e.target.value = "";
    if (!f) return;
    if (!/^image\/(jpeg|png|webp)$/.test(f.type)) return setFileError(t("chat.file_type_error"));
    if (f.size > MAX_IMAGE_BYTES) return setFileError(t("chat.file_size_error"));
    setFileError(null);
    setFile(f);
  }

  const urgent = windowOpen && secondsLeft < 3600;

  return (
    <div className="border-t border-gray-100 bg-white px-3 sm:px-4 py-3 space-y-2">
      {/* 24h window timer */}
      <div
        className={`flex items-center gap-1.5 text-xs font-medium ${
          !windowOpen ? "text-red-600" : urgent ? "text-amber-700" : "text-gray-500"
        }`}
      >
        <Clock size={13} />
        {blocked
          ? t("chat.opted_out")
          : windowOpen
          ? t("chat.window_closes_in", { time: durationShort(secondsLeft) })
          : t("chat.window_closed")}
      </div>

      {!windowOpen && !blocked && (
        detail.channel === "whatsapp" ? (
          <TemplatePicker templates={templates} loading={templatesLoading} onSend={onSendTemplate} />
        ) : (
          <p className="text-xs text-gray-500">{t("chat.window_closed_no_templates")}</p>
        )
      )}

      {fileError && <p className="text-xs text-red-600">{fileError}</p>}
      {preview && (
        <div className="relative inline-block">
          <img src={preview} alt="" className="h-20 rounded-lg border border-gray-200 object-cover" />
          <button
            onClick={() => setFile(null)}
            className="absolute -top-2 -right-2 w-5 h-5 rounded-full bg-gray-800 text-white flex items-center justify-center"
            aria-label={t("chat.remove_attachment")}
          >
            <X size={11} />
          </button>
        </div>
      )}

      <div className="flex items-end gap-2">
        <input ref={fileInput} type="file" accept="image/jpeg,image/png,image/webp" className="hidden" onChange={onPick} />
        <button
          onClick={() => fileInput.current?.click()}
          disabled={!canType}
          className="w-10 h-10 shrink-0 rounded-xl border border-gray-200 text-gray-500 flex items-center justify-center hover:bg-brand-bg disabled:opacity-40"
          aria-label={t("chat.attach_image")}
          title={t("chat.attach_image")}
        >
          <Paperclip size={17} />
        </button>
        <textarea
          ref={area}
          rows={1}
          value={text}
          disabled={!canType}
          onChange={(e) => {
            setText(e.target.value);
            e.target.style.height = "auto";
            e.target.style.height = `${Math.min(e.target.scrollHeight, 120)}px`;
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              submit();
            }
          }}
          placeholder={canType ? (file ? t("chat.caption_placeholder") : t("chat.type_message")) : t("chat.composer_disabled")}
          aria-label={t("chat.type_message")}
          className="flex-1 border border-gray-200 rounded-xl px-3.5 py-2.5 text-sm resize-none bg-brand-bg focus:outline-none focus:ring-2 focus:ring-brand-primary disabled:opacity-60 disabled:cursor-not-allowed"
          style={{ minHeight: 42 }}
        />
        <button
          onClick={submit}
          disabled={!canType || (!text.trim() && !file)}
          className="w-10 h-10 shrink-0 rounded-xl bg-brand-primary text-white flex items-center justify-center hover:bg-brand-primaryDark disabled:opacity-40 transition-colors"
          aria-label={t("chat.send")}
        >
          <Send size={16} />
        </button>
      </div>
      <p className="hidden sm:block text-[10px] text-gray-400">{t("chat.composer_hint")}</p>
    </div>
  );
}
