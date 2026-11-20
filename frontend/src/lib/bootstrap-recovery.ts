// Keep this function self-contained: the server embeds it before application chunks.
export function installBootstrapRecovery(target: Window, reload: () => void) {
  const key = "ai-desktop-agent-bootstrap-retry";
  const document = target.document;
  let failed = false;
  let timer: number | undefined;

  function clearRetry() {
    try { target.sessionStorage.removeItem(key); }
    catch { /* A manual reload still works when storage is unavailable. */ }
  }

  function showFailure() {
    const panel = document.createElement("main");
    panel.className = "login-screen";
    panel.innerHTML = '<section class="login-form"><h1>AI Desktop Agent</h1><p role="alert">画面を読み込めませんでした</p><button type="button">再読み込み</button></section>';
    const button = panel.querySelector("button")!;
    button.addEventListener("click", () => {
      button.disabled = true;
      clearRetry();
      reload();
    });
    document.body.replaceChildren(panel);
  }

  function onError(event: Event) {
    if (failed) return;
    const script = event.target;
    if (!(script instanceof HTMLScriptElement) || !script.src.startsWith(target.location.origin + "/_next/static/")) return;
    failed = true;
    let retry = false;
    try {
      retry = target.sessionStorage.getItem(key) === null;
      if (retry) target.sessionStorage.setItem(key, "1");
    } catch { retry = false; }
    if (retry) {
      timer = target.setTimeout(reload, 1000);
    } else if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", showFailure, { once: true });
    } else {
      showFailure();
    }
  }

  function cleanup() {
    target.clearTimeout(timer);
    target.removeEventListener("error", onError, true);
    target.removeEventListener("ai-desktop-agent-ready", onReady);
    document.removeEventListener("DOMContentLoaded", showFailure);
  }

  function onReady() {
    cleanup();
    clearRetry();
  }

  target.addEventListener("error", onError, true);
  target.addEventListener("ai-desktop-agent-ready", onReady, { once: true });
  return cleanup;
}
