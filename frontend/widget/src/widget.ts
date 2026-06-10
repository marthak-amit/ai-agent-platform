/**
 * AI Agent Chat Widget
 *
 * Embeds as a single JS file. Client adds one line to their website:
 *   <script src="https://yourplatform.com/widget.min.js"
 *           data-api-key="vp_xxxxx"></script>
 *
 * The widget:
 *   1. Reads data-api-key and derives the backend URL from the script src.
 *   2. Fetches /widget/config/{api_key} for business name + welcome message.
 *   3. Shows a floating chat bubble (bottom-right).
 *   4. Opens a chat window on click.
 *   5. POSTs messages to /widget/message and renders AI replies.
 *   6. Persists session_id in localStorage so history survives page reloads.
 */

(function () {
  // ── Config ────────────────────────────────────────────────────────────────

  /** Find the <script data-api-key="…"> tag that loaded this file. */
  function getScriptTag(): HTMLScriptElement | null {
    // document.currentScript works for synchronous scripts.
    if (document.currentScript) {
      return document.currentScript as HTMLScriptElement;
    }
    // Fallback: last <script> tag with data-api-key (covers async/defer).
    const tags = document.querySelectorAll<HTMLScriptElement>(
      "script[data-api-key]"
    );
    return tags.length > 0 ? tags[tags.length - 1] : null;
  }

  const scriptTag = getScriptTag();
  if (!scriptTag) return;

  const API_KEY = scriptTag.getAttribute("data-api-key") || "";
  if (!API_KEY) {
    console.warn("[AI Widget] No data-api-key found on script tag.");
    return;
  }

  // Derive base URL from the script src:
  //   https://api.yourplatform.com/widget.min.js → https://api.yourplatform.com
  const BACKEND_BASE = (scriptTag.src || "").replace(/\/widget\.min\.js.*$/, "");

  const SESSION_KEY = `vp_session_${API_KEY}`;

  // ── State ─────────────────────────────────────────────────────────────────

  interface Message {
    role: "user" | "bot";
    text: string;
  }

  interface Config {
    business_name: string;
    welcome_message: string;
    brand_color: string;
  }

  let isOpen = false;
  let isLoading = false;
  let messages: Message[] = [];
  let cfg: Config = {
    business_name: "AI Assistant",
    welcome_message: "Hi! How can I help you today?",
    brand_color: "#6366f1",
  };

  function getSessionId(): string {
    let id = localStorage.getItem(SESSION_KEY);
    if (!id) {
      id =
        typeof crypto !== "undefined" && crypto.randomUUID
          ? crypto.randomUUID()
          : Math.random().toString(36).slice(2) + Date.now().toString(36);
      localStorage.setItem(SESSION_KEY, id);
    }
    return id;
  }

  // ── CSS (injected once) ────────────────────────────────────────────────────

  function injectStyles(brandColor: string): void {
    const existing = document.getElementById("vp-widget-styles");
    if (existing) {
      existing.remove();
    }
    const style = document.createElement("style");
    style.id = "vp-widget-styles";
    style.textContent = `
      #vp-widget-btn {
        position: fixed;
        bottom: 24px;
        right: 24px;
        width: 56px;
        height: 56px;
        border-radius: 50%;
        background: ${brandColor};
        border: none;
        cursor: pointer;
        box-shadow: 0 4px 16px rgba(0,0,0,0.22);
        display: flex;
        align-items: center;
        justify-content: center;
        z-index: 2147483640;
        transition: transform 0.18s ease, box-shadow 0.18s ease;
      }
      #vp-widget-btn:hover {
        transform: scale(1.08);
        box-shadow: 0 6px 22px rgba(0,0,0,0.28);
      }
      #vp-widget-btn svg {
        width: 26px;
        height: 26px;
        fill: #fff;
        pointer-events: none;
      }

      #vp-widget-window {
        position: fixed;
        bottom: 92px;
        right: 24px;
        width: 360px;
        height: 520px;
        background: #fff;
        border-radius: 16px;
        box-shadow: 0 8px 40px rgba(0,0,0,0.18);
        display: flex;
        flex-direction: column;
        z-index: 2147483639;
        overflow: hidden;
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto,
                     Helvetica, Arial, sans-serif;
        font-size: 14px;
        transition: opacity 0.2s ease, transform 0.2s ease;
      }
      #vp-widget-window.vp-hidden {
        opacity: 0;
        transform: translateY(12px) scale(0.97);
        pointer-events: none;
      }

      #vp-widget-header {
        background: ${brandColor};
        color: #fff;
        padding: 14px 16px;
        display: flex;
        align-items: center;
        gap: 10px;
        flex-shrink: 0;
      }
      #vp-widget-header .vp-avatar {
        width: 34px;
        height: 34px;
        border-radius: 50%;
        background: rgba(255,255,255,0.25);
        display: flex;
        align-items: center;
        justify-content: center;
        flex-shrink: 0;
      }
      #vp-widget-header .vp-avatar svg {
        width: 18px;
        height: 18px;
        fill: #fff;
      }
      #vp-widget-header .vp-name {
        font-weight: 600;
        font-size: 15px;
        flex: 1;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }
      #vp-widget-header .vp-status {
        font-size: 11px;
        opacity: 0.85;
      }
      #vp-widget-close {
        background: none;
        border: none;
        cursor: pointer;
        color: #fff;
        padding: 4px;
        border-radius: 4px;
        display: flex;
        align-items: center;
        opacity: 0.85;
        flex-shrink: 0;
      }
      #vp-widget-close:hover { opacity: 1; }
      #vp-widget-close svg { width: 18px; height: 18px; fill: #fff; }

      #vp-widget-messages {
        flex: 1;
        overflow-y: auto;
        padding: 16px;
        display: flex;
        flex-direction: column;
        gap: 10px;
        background: #f8f9fa;
        scroll-behavior: smooth;
      }
      #vp-widget-messages::-webkit-scrollbar { width: 4px; }
      #vp-widget-messages::-webkit-scrollbar-thumb {
        background: #ddd;
        border-radius: 2px;
      }

      .vp-msg {
        max-width: 82%;
        padding: 10px 13px;
        border-radius: 14px;
        line-height: 1.45;
        word-wrap: break-word;
        white-space: pre-wrap;
        font-size: 13.5px;
      }
      .vp-msg.vp-bot {
        background: #fff;
        border: 1px solid #e8e8e8;
        align-self: flex-start;
        border-bottom-left-radius: 4px;
        color: #222;
      }
      .vp-msg.vp-user {
        background: ${brandColor};
        color: #fff;
        align-self: flex-end;
        border-bottom-right-radius: 4px;
      }
      .vp-msg.vp-typing {
        background: #fff;
        border: 1px solid #e8e8e8;
        align-self: flex-start;
        border-bottom-left-radius: 4px;
        color: #999;
        font-style: italic;
      }

      #vp-widget-input-row {
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 12px 14px;
        border-top: 1px solid #eee;
        background: #fff;
        flex-shrink: 0;
      }
      #vp-widget-input {
        flex: 1;
        border: 1px solid #e0e0e0;
        border-radius: 22px;
        padding: 9px 14px;
        font-size: 13.5px;
        font-family: inherit;
        outline: none;
        background: #fafafa;
        transition: border-color 0.15s;
        resize: none;
        min-height: 38px;
        max-height: 90px;
        line-height: 1.45;
        color: #222;
      }
      #vp-widget-input:focus {
        border-color: ${brandColor};
        background: #fff;
      }
      #vp-widget-input::placeholder { color: #aaa; }
      #vp-widget-send {
        width: 38px;
        height: 38px;
        border-radius: 50%;
        background: ${brandColor};
        border: none;
        cursor: pointer;
        display: flex;
        align-items: center;
        justify-content: center;
        flex-shrink: 0;
        transition: opacity 0.15s;
      }
      #vp-widget-send:disabled { opacity: 0.45; cursor: default; }
      #vp-widget-send svg { width: 17px; height: 17px; fill: #fff; }

      #vp-widget-branding {
        text-align: center;
        font-size: 11px;
        color: #bbb;
        padding: 6px 0 8px;
        background: #fff;
        flex-shrink: 0;
      }
      #vp-widget-branding a {
        color: #bbb;
        text-decoration: none;
      }

      @media (max-width: 420px) {
        #vp-widget-window {
          right: 0;
          bottom: 0;
          width: 100vw;
          height: 100dvh;
          border-radius: 0;
        }
        #vp-widget-btn {
          bottom: 16px;
          right: 16px;
        }
      }
    `;
    document.head.appendChild(style);
  }

  // ── SVG icons (inline) ────────────────────────────────────────────────────

  const ICON_CHAT = `<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
    <path d="M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zm0
    14H6l-2 2V4h16v12z"/>
  </svg>`;

  const ICON_CLOSE = `<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
    <path d="M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59
    6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z"/>
  </svg>`;

  const ICON_SEND = `<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
    <path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z"/>
  </svg>`;

  const ICON_BOT = `<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
    <path d="M12 2a2 2 0 012 2c0 .74-.4 1.39-1 1.73V7h1a7 7 0 017 7H4a7 7 0 017-7h1V5.73A2 2 0
    0112 2zm-5 9v2H5v-2h2zm12 0v2h-2v-2h2zM7 19.5a1.5 1.5 0 003 0v-.5H7v.5zm7 0a1.5 1.5 0 003
    0v-.5h-3v.5z"/>
  </svg>`;

  // ── DOM: build chat window ────────────────────────────────────────────────

  let windowEl: HTMLDivElement | null = null;
  let messagesEl: HTMLDivElement | null = null;
  let inputEl: HTMLTextAreaElement | null = null;
  let sendBtn: HTMLButtonElement | null = null;

  function buildWindow(): void {
    if (windowEl) return;

    windowEl = document.createElement("div");
    windowEl.id = "vp-widget-window";
    windowEl.className = "vp-hidden";
    windowEl.setAttribute("role", "dialog");
    windowEl.setAttribute("aria-label", "Chat with " + cfg.business_name);

    // Header
    const header = document.createElement("div");
    header.id = "vp-widget-header";
    header.innerHTML = `
      <div class="vp-avatar">${ICON_BOT}</div>
      <div>
        <div class="vp-name">${escHtml(cfg.business_name)}</div>
        <div class="vp-status">Powered by AI · Online</div>
      </div>
    `;
    const closeBtn = document.createElement("button");
    closeBtn.id = "vp-widget-close";
    closeBtn.title = "Close";
    closeBtn.innerHTML = ICON_CLOSE;
    closeBtn.addEventListener("click", toggleChat);
    header.appendChild(closeBtn);
    windowEl.appendChild(header);

    // Messages area
    messagesEl = document.createElement("div");
    messagesEl.id = "vp-widget-messages";
    windowEl.appendChild(messagesEl);

    // Input row
    const inputRow = document.createElement("div");
    inputRow.id = "vp-widget-input-row";

    inputEl = document.createElement("textarea");
    inputEl.id = "vp-widget-input";
    inputEl.placeholder = "Type a message…";
    inputEl.rows = 1;
    inputEl.setAttribute("aria-label", "Message input");
    inputEl.addEventListener("keydown", (e: KeyboardEvent) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        handleSend();
      }
    });
    // Auto-grow up to max-height
    inputEl.addEventListener("input", () => {
      if (!inputEl) return;
      inputEl.style.height = "auto";
      inputEl.style.height = Math.min(inputEl.scrollHeight, 90) + "px";
    });

    sendBtn = document.createElement("button");
    sendBtn.id = "vp-widget-send";
    sendBtn.title = "Send";
    sendBtn.setAttribute("aria-label", "Send message");
    sendBtn.innerHTML = ICON_SEND;
    sendBtn.addEventListener("click", handleSend);

    inputRow.appendChild(inputEl);
    inputRow.appendChild(sendBtn);
    windowEl.appendChild(inputRow);

    // Branding
    const branding = document.createElement("div");
    branding.id = "vp-widget-branding";
    branding.innerHTML = `Powered by <a href="https://visionplus.ai" target="_blank" rel="noopener">VisionPlus AI</a>`;
    windowEl.appendChild(branding);

    document.body.appendChild(windowEl);

    // Show the welcome message once the window is built
    if (messages.length === 0) {
      addMessage("bot", cfg.welcome_message);
    } else {
      renderAllMessages();
    }
  }

  // ── DOM: floating button ──────────────────────────────────────────────────

  function buildButton(): HTMLButtonElement {
    const btn = document.createElement("button");
    btn.id = "vp-widget-btn";
    btn.title = "Chat with us";
    btn.setAttribute("aria-label", "Open chat");
    btn.innerHTML = ICON_CHAT;
    btn.addEventListener("click", toggleChat);
    return btn;
  }

  // ── Toggle open/close ─────────────────────────────────────────────────────

  function toggleChat(): void {
    isOpen = !isOpen;
    if (!windowEl) {
      buildWindow();
    }
    if (isOpen) {
      windowEl!.classList.remove("vp-hidden");
      requestAnimationFrame(() => inputEl?.focus());
    } else {
      windowEl!.classList.add("vp-hidden");
    }
  }

  // ── Message rendering ─────────────────────────────────────────────────────

  function escHtml(str: string): string {
    return str
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function addMessage(role: "user" | "bot", text: string): void {
    messages.push({ role, text });
    if (messagesEl) {
      appendBubble(role, text);
      scrollToBottom();
    }
  }

  function appendBubble(role: "user" | "bot", text: string): void {
    if (!messagesEl) return;
    const div = document.createElement("div");
    div.className = `vp-msg ${role === "bot" ? "vp-bot" : "vp-user"}`;
    div.textContent = text;
    messagesEl.appendChild(div);
  }

  function renderAllMessages(): void {
    if (!messagesEl) return;
    messagesEl.innerHTML = "";
    messages.forEach((m) => appendBubble(m.role, m.text));
    scrollToBottom();
  }

  function showTypingIndicator(): HTMLDivElement | null {
    if (!messagesEl) return null;
    const div = document.createElement("div");
    div.className = "vp-msg vp-typing";
    div.textContent = "Typing…";
    div.id = "vp-widget-typing";
    messagesEl.appendChild(div);
    scrollToBottom();
    return div;
  }

  function removeTypingIndicator(): void {
    document.getElementById("vp-widget-typing")?.remove();
  }

  function scrollToBottom(): void {
    if (messagesEl) {
      messagesEl.scrollTop = messagesEl.scrollHeight;
    }
  }

  function setInputEnabled(enabled: boolean): void {
    if (inputEl) inputEl.disabled = !enabled;
    if (sendBtn) sendBtn.disabled = !enabled;
  }

  // ── Send message ──────────────────────────────────────────────────────────

  async function handleSend(): Promise<void> {
    if (!inputEl || isLoading) return;
    const text = inputEl.value.trim();
    if (!text) return;

    inputEl.value = "";
    inputEl.style.height = "auto";
    addMessage("user", text);
    isLoading = true;
    setInputEnabled(false);
    showTypingIndicator();

    try {
      const res = await fetch(`${BACKEND_BASE}/widget/message`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          api_key: API_KEY,
          session_id: getSessionId(),
          message: text,
        }),
      });

      removeTypingIndicator();

      if (res.ok) {
        const data = (await res.json()) as { reply: string };
        addMessage("bot", data.reply);
      } else if (res.status === 403) {
        addMessage(
          "bot",
          "This feature requires an upgraded plan. Please contact the business owner."
        );
      } else {
        addMessage("bot", "Something went wrong. Please try again.");
      }
    } catch {
      removeTypingIndicator();
      addMessage("bot", "Could not connect. Please check your internet connection.");
    } finally {
      isLoading = false;
      setInputEnabled(true);
      requestAnimationFrame(() => inputEl?.focus());
    }
  }

  // ── Load config from backend ──────────────────────────────────────────────

  async function loadConfig(): Promise<void> {
    try {
      const res = await fetch(`${BACKEND_BASE}/widget/config/${API_KEY}`);
      if (!res.ok) return;
      const data = (await res.json()) as Config;
      cfg = data;
      // Rebuild styles with potentially new brand color
      injectStyles(cfg.brand_color);
      // Update button color
      const btn = document.getElementById("vp-widget-btn");
      if (btn) {
        (btn as HTMLButtonElement).style.background = cfg.brand_color;
      }
      // Update header if window already open
      const nameEl = document.querySelector("#vp-widget-header .vp-name");
      if (nameEl) nameEl.textContent = cfg.business_name;
    } catch {
      // Config load is non-critical — widget still works with defaults
    }
  }

  // ── Init ──────────────────────────────────────────────────────────────────

  function init(): void {
    injectStyles(cfg.brand_color);
    const btn = buildButton();
    document.body.appendChild(btn);
    loadConfig();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
