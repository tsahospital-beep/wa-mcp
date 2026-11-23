const root = document.documentElement;
const systemTheme = matchMedia("(prefers-color-scheme: dark)");
const themeButton = document.querySelector(".theme");
const navigation = document.querySelector(".navigation");
const menuButton = document.querySelector(".menu-button");

function currentTheme() {
  return root.dataset.theme || (systemTheme.matches ? "dark" : "light");
}

function describeTheme() {
  themeButton.setAttribute("aria-label", currentTheme() === "dark" ? "Switch to light mode" : "Switch to dark mode");
}

themeButton.addEventListener("click", () => {
  root.dataset.theme = currentTheme() === "dark" ? "light" : "dark";
  try { localStorage.setItem("wa-theme", root.dataset.theme); } catch {}
  describeTheme();
});
systemTheme.addEventListener("change", describeTheme);
describeTheme();

function setMenu(open) {
  navigation.classList.toggle("open", open);
  menuButton.setAttribute("aria-expanded", String(open));
  menuButton.setAttribute("aria-label", open ? "Close navigation" : "Open navigation");
}

menuButton.addEventListener("click", () => setMenu(!navigation.classList.contains("open")));
navigation.addEventListener("click", (event) => {
  if (event.target.closest("a")) setMenu(false);
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && navigation.classList.contains("open")) {
    setMenu(false);
    menuButton.focus();
  }
});
document.addEventListener("click", (event) => {
  if (!event.target.closest(".bar")) setMenu(false);
});
matchMedia("(min-width: 801px)").addEventListener("change", () => setMenu(false));

async function updateStars() {
  const badge = document.querySelector(".star-count");
  function show(value, cached) {
    badge.textContent = value.toLocaleString("en-US");
    badge.title = (cached ? "Last available count: " : "GitHub stars: ") + badge.textContent;
    badge.hidden = false;
  }
  try {
    const saved = JSON.parse(sessionStorage.getItem("wa-stars"));
    if (saved && Number.isSafeInteger(saved.count) && saved.count >= 0) {
      show(saved.count, true);
      if (Date.now() - saved.time < 300000) return;
    }
  } catch {}
  try {
    const response = await fetch("https://api.github.com/repos/tsahospital-beep/wa-mcp", {
      signal: AbortSignal.timeout(4500),
      credentials: "omit",
      referrerPolicy: "no-referrer"
    });
    if (!response.ok) return;
    const { stargazers_count: count } = await response.json();
    if (!Number.isSafeInteger(count) || count < 0) return;
    show(count, false);
    try { sessionStorage.setItem("wa-stars", JSON.stringify({ count, time: Date.now() })); } catch {}
  } catch {
    // The repository link works with a cached count or without a badge.
  }
}
updateStars();

const copyButton = document.getElementById("copy");
if (copyButton) {
  const status = document.querySelector(".copy-feedback");
  let clearFeedback;
  copyButton.addEventListener("click", async () => {
    const command = document.getElementById("command");
    try {
      await navigator.clipboard.writeText(command.textContent);
      status.textContent = "Copied";
    } catch {
      const range = document.createRange();
      range.selectNodeContents(command);
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      status.textContent = "Command selected. Use Copy.";
    }
    clearTimeout(clearFeedback);
    clearFeedback = setTimeout(() => { status.textContent = ""; }, 2600);
  });
}

const conversation = document.querySelector(".conversation");
if (conversation) {
  const prompt = document.getElementById("prompt");
  const toolName = document.getElementById("tool-name");
  const resultTitle = document.getElementById("result-title");
  const resultList = document.getElementById("result-list");
  const examples = {
    day: {
      prompt: prompt.textContent,
      tool: toolName.textContent,
      title: resultTitle.textContent,
      lines: Array.from(resultList.children, item => item.textContent)
    },
    search: {
      prompt: "Find the latest invoice request.",
      tool: "list_messages",
      title: "One matching request.",
      lines: ["Yesterday at 16:40", "A client asked for the updated invoice.", "The request is still open."]
    },
    voice: {
      prompt: "Transcribe the book club voice note.",
      tool: "transcribe_chat_audio",
      title: "Voice note transcript",
      lines: ["Book club", "“Which day works for the next meeting?”"]
    }
  };
  function replay() {
    conversation.classList.remove("play");
    // Restart the finite entrance sequence after a user action.
    void conversation.offsetWidth;
    conversation.classList.add("play");
  }
  conversation.setAttribute("aria-live", "polite");
  conversation.setAttribute("aria-atomic", "true");
  const buttons = document.querySelectorAll("[data-example]");
  for (const button of buttons) {
    button.addEventListener("click", () => {
      const example = examples[button.dataset.example];
      for (const option of buttons) option.setAttribute("aria-pressed", String(option === button));
      prompt.textContent = example.prompt;
      toolName.textContent = example.tool;
      resultTitle.textContent = example.title;
      resultList.replaceChildren(...example.lines.map(text => {
        const item = document.createElement("li");
        item.textContent = text;
        return item;
      }));
      replay();
    });
  }
  document.getElementById("replay").addEventListener("click", replay);
  replay();
}

