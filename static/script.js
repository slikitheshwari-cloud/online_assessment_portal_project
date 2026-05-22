// CDC Assessment — Shared JS Utilities

/**
 * Prevent accidental navigation during exam.
 * Call enableExamGuard() on the exam page.
 */
function enableExamGuard() {
  window.onbeforeunload = () => 'Your exam is in progress. Are you sure?';
}

function disableExamGuard() {
  window.onbeforeunload = null;
}

/**
 * Auto-save essay text to localStorage every 30 seconds.
 */
function startAutoSave(textareaId, key) {
  setInterval(() => {
    const val = document.getElementById(textareaId)?.value;
    if (val !== undefined) localStorage.setItem(key, val);
  }, 30000);
}

function restoreAutoSave(textareaId, key) {
  const saved = localStorage.getItem(key);
  if (saved) document.getElementById(textareaId).value = saved;
}
