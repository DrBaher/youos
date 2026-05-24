const api = globalThis.browser ?? globalThis.chrome;
const DEFAULT_BASE = "http://127.0.0.1:8765";
const baseInput = document.getElementById("base");
const tokenInput = document.getElementById("token");
const saved = document.getElementById("saved");

api.storage.sync.get(["youosBaseUrl", "youosToken"]).then(({ youosBaseUrl, youosToken }) => {
  baseInput.value = youosBaseUrl || DEFAULT_BASE;
  tokenInput.value = youosToken || "";
});

document.getElementById("save").addEventListener("click", async () => {
  const url = baseInput.value.trim().replace(/\/+$/, "") || DEFAULT_BASE;
  const token = tokenInput.value.trim();
  await api.storage.sync.set({ youosBaseUrl: url, youosToken: token });
  baseInput.value = url;
  saved.textContent = "Saved ✓";
  setTimeout(() => (saved.textContent = ""), 2000);
});
