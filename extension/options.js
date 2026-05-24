const DEFAULT_BASE = "http://127.0.0.1:8765";
const baseInput = document.getElementById("base");
const saved = document.getElementById("saved");

chrome.storage.sync.get("youosBaseUrl").then(({ youosBaseUrl }) => {
  baseInput.value = youosBaseUrl || DEFAULT_BASE;
});

document.getElementById("save").addEventListener("click", async () => {
  let url = baseInput.value.trim().replace(/\/+$/, "") || DEFAULT_BASE;
  await chrome.storage.sync.set({ youosBaseUrl: url });
  baseInput.value = url;
  saved.textContent = "Saved ✓";
  setTimeout(() => (saved.textContent = ""), 2000);
});
