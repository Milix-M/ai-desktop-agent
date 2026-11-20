interface PressedKey {
  target: EventTarget;
  event: KeyboardEventInit;
}

// Release through noVNC's DOM handlers while input is still enabled. Setting
// viewOnly first suppresses the key-up events from noVNC's keyboard cleanup.
export function trackVncInput(target: HTMLElement, connection: { readonly viewOnly: boolean }) {
  const keys = new Map<string, PressedKey>();
  let pointer: { clientX: number; clientY: number } | null = null;
  const keyId = (event: KeyboardEvent) => event.code && event.code !== "Unidentified"
    ? event.code : `${event.keyCode}:${event.key}`;

  function keyDown(event: KeyboardEvent) {
    if (connection.viewOnly) return;
    keys.set(keyId(event), { target: event.target!, event: {
      key: event.key, code: event.code, location: event.location, keyCode: event.keyCode,
      bubbles: true, cancelable: true,
    } });
  }
  function keyUp(event: KeyboardEvent) { keys.delete(keyId(event)); }
  function mouseButton(event: MouseEvent) {
    if (connection.viewOnly) return;
    pointer = event.buttons ? { clientX: event.clientX, clientY: event.clientY } : null;
  }
  function mouseMove(event: MouseEvent) {
    if (pointer) pointer = { clientX: event.clientX, clientY: event.clientY };
  }
  function release() {
    const pressed = [...keys.values()];
    keys.clear();
    for (const key of pressed) key.target.dispatchEvent(new KeyboardEvent("keyup", key.event));
    if (pointer) {
      const position = pointer;
      pointer = null;
      // noVNC captures a pressed mouse on window. Its handler forwards this
      // release to the canvas and removes the full-page capture overlay.
      window.dispatchEvent(new MouseEvent("mouseup", {
        ...position, button: 0, buttons: 0, bubbles: true, cancelable: true,
      }));
    }
  }
  target.addEventListener("keydown", keyDown, true);
  target.addEventListener("keyup", keyUp, true);
  target.addEventListener("mousedown", mouseButton, true);
  target.addEventListener("mouseup", mouseButton, true);
  target.addEventListener("mousemove", mouseMove, true);
  window.addEventListener("blur", release);

  return {
    release,
    dispose() {
      release();
      target.removeEventListener("keydown", keyDown, true);
      target.removeEventListener("keyup", keyUp, true);
      target.removeEventListener("mousedown", mouseButton, true);
      target.removeEventListener("mouseup", mouseButton, true);
      target.removeEventListener("mousemove", mouseMove, true);
      window.removeEventListener("blur", release);
    },
  };
}
