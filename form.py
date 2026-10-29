# Embedded copy of contribute.html. The server replaces __API_URL__ and __RAW_BASE__ at startup.
FORM_TEMPLATE = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Suggest an edit - GDGOC Unilorin Resource Hub</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Figtree:wght@400;500;700&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #fbfbf9; --surface: #ffffff; --ink: #1d2433; --muted: #5d6677;
    --line: #d9dde4; --accent: #1a6ef0; --accent-ink: #ffffff;
    --ok: #137a3f; --err: #b3261e;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #12151c; --surface: #1a1f29; --ink: #eceff5; --muted: #98a2b3;
      --line: #2b3342; --accent: #6ea2ff; --accent-ink: #0d1626;
      --ok: #5fd08a; --err: #ff8a80;
    }
  }
  * { box-sizing: border-box; }
  html { background: var(--bg); }
  body {
    margin: 0; color: var(--ink); background: var(--bg);
    font: 400 16px/1.55 "Figtree", system-ui, -apple-system, "Segoe UI", sans-serif;
    padding: 48px 20px 64px;
    padding-top: max(48px, env(safe-area-inset-top));
    padding-bottom: max(64px, env(safe-area-inset-bottom));
  }
  main { max-width: 700px; margin: 0 auto; }
  h1 { font-size: 2rem; line-height: 1.15; margin: 0 0 10px; letter-spacing: -0.02em; }
  .lede { color: var(--muted); margin: 0 0 32px; max-width: 60ch; }
  label { display: block; font-weight: 700; margin: 0 0 6px; }
  .hint { color: var(--muted); font-weight: 400; font-size: .9rem; }
  .field { margin-bottom: 22px; }
  input, textarea {
    width: 100%; font: inherit; color: var(--ink); background: var(--surface);
    border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px;
  }
  textarea {
    min-height: 320px; resize: vertical; tab-size: 2;
    font: 400 14px/1.6 ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  }
  input:focus-visible, textarea:focus-visible, button:focus-visible, a:focus-visible {
    outline: 3px solid var(--accent); outline-offset: 2px;
  }
  .note { font-size: .9rem; color: var(--muted); margin-top: 6px; min-height: 1.4em; }
  button {
    font: 700 1rem "Figtree", system-ui, sans-serif; background: var(--accent); color: var(--accent-ink);
    border: 0; border-radius: 8px; padding: 12px 22px; cursor: pointer;
  }
  button:disabled { opacity: .55; cursor: progress; }
  .status { margin-top: 20px; padding: 14px 16px; border-radius: 8px; border: 1px solid var(--line); background: var(--surface); }
  .status[hidden] { display: none; }
  .status.ok { border-color: var(--ok); }
  .status.err { border-color: var(--err); }
  .status strong { display: block; margin-bottom: 2px; }
  .status.ok strong { color: var(--ok); }
  .status.err strong { color: var(--err); }
  .hp { position: absolute; left: -9999px; width: 1px; height: 1px; overflow: hidden; }
  a { color: var(--accent); }
  @media (prefers-reduced-motion: no-preference) { button { transition: opacity .15s; } }
</style>
</head>
<body>
<main>
  <h1>Suggest an edit</h1>
  <p class="lede">Fix a typo, update a link, or add a page. You don't need a GitHub account. A reviewer checks every suggestion before it goes live.</p>

  <form id="form" novalidate>
    <div class="field">
      <label for="page">Page <span class="hint">(for example careers/cv-tips)</span></label>
      <input id="page" name="page" required autocomplete="off" spellcheck="false">
      <div class="note" id="pageNote"></div>
    </div>

    <div class="field">
      <label for="content">Page content <span class="hint">(Markdown / MDX)</span></label>
      <textarea id="content" name="content" required spellcheck="true"></textarea>
    </div>

    <div class="field">
      <label for="summary">What did you change? <span class="hint">(one line)</span></label>
      <input id="summary" name="summary" maxlength="200" required placeholder="Fix broken link in CV section">
    </div>

    <div class="field">
      <label for="name">Your name <span class="hint">(optional, credited to the reviewer)</span></label>
      <input id="name" name="name" maxlength="80" autocomplete="name">
    </div>

    <div class="hp" aria-hidden="true">
      <label for="website">Leave this field empty</label>
      <input id="website" name="website" tabindex="-1" autocomplete="off">
    </div>

    <button type="submit" id="send">Send for review</button>
  </form>

  <div class="status" id="status" role="status" aria-live="polite" hidden></div>
</main>

<script>
  // Filled in by the server at startup
  const API_URL  = "__API_URL__";
  const RAW_BASE = "__RAW_BASE__";

  const $ = (id) => document.getElementById(id);
  const form = $("form"), pageEl = $("page"), contentEl = $("content");
  const pageNote = $("pageNote"), statusEl = $("status"), sendBtn = $("send");

  const toPath = (v) => {
    v = v.trim().replace(/^\/+/, "");
    return v && !v.endsWith(".mdx") ? v + ".mdx" : v;
  };

  function showStatus(kind, title, html) {
    statusEl.className = "status " + kind;
    statusEl.innerHTML = "<strong></strong><span></span>";
    statusEl.querySelector("strong").textContent = title;
    statusEl.querySelector("span").innerHTML = html;
    statusEl.hidden = false;
    statusEl.scrollIntoView({ block: "nearest" });
  }

  async function loadPage() {
    const path = toPath(pageEl.value);
    if (!path) return;
    pageNote.textContent = "Loading current content...";
    try {
      const res = await fetch(RAW_BASE + path);
      if (res.ok) {
        contentEl.value = await res.text();
        pageNote.textContent = "Loaded the current page. Edit it below.";
      } else if (res.status === 404) {
        pageNote.textContent = "No page found at this path. Submitting will propose a new page.";
      } else {
        pageNote.textContent = "Couldn't load the current content. You can still paste your version below.";
      }
    } catch {
      pageNote.textContent = "Couldn't load the current content. You can still paste your version below.";
    }
  }

  pageEl.addEventListener("change", loadPage);

  const preset = new URLSearchParams(location.search).get("page");
  if (preset) { pageEl.value = preset; loadPage(); }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    statusEl.hidden = true;

    const path = toPath(pageEl.value);
    if (!/^[A-Za-z0-9_\-\/]+\.mdx$/.test(path))
      return showStatus("err", "Check the page path", "Use letters, numbers, dashes and slashes only, like careers/cv-tips.");
    if (!contentEl.value.trim())
      return showStatus("err", "Content is empty", "Add the page content before sending.");
    if (!$("summary").value.trim())
      return showStatus("err", "Add a short summary", "Say in one line what you changed.");

    sendBtn.disabled = true;
    sendBtn.textContent = "Sending...";
    try {
      const res = await fetch(API_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          path,
          content: contentEl.value,
          summary: $("summary").value,
          name: $("name").value,
          website: $("website").value,
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (res.ok && data.ok) {
        const link = data.pr_url
          ? ` Track it <a href="${encodeURI(data.pr_url)}" target="_blank" rel="noopener">here</a>.`
          : "";
        showStatus("ok", "Sent for review", "A reviewer will check your edit." + link);
        form.reset();
        pageNote.textContent = "";
      } else if (res.status === 429) {
        showStatus("err", "Too many suggestions", "Wait an hour, then try again.");
      } else {
        showStatus("err", "Couldn't send your edit", (data.error || "The server rejected the request") + ". Check your entries and try again.");
      }
    } catch {
      showStatus("err", "Couldn't reach the server", "Check your connection and try again. Your text is still in the form.");
    } finally {
      sendBtn.disabled = false;
      sendBtn.textContent = "Send for review";
    }
  });
</script>
</body>
</html>
'''
