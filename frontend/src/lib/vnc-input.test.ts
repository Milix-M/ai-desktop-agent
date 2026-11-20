import { afterEach, expect, test, vi } from "vitest";
import { trackVncInput } from "./vnc-input";

const disposals: (() => void)[] = [];
function setup(viewOnly = false) {
  const target = document.createElement("div");
  const canvas = document.createElement("canvas");
  target.appendChild(canvas); document.body.appendChild(target);
  const connection = { viewOnly };
  const input = trackVncInput(target, connection);
  disposals.push(() => { input.dispose(); target.remove(); });
  const up = vi.fn<(event: KeyboardEvent) => void>(); canvas.addEventListener("keyup", up);
  const pointerUp = vi.fn<(event: MouseEvent) => void>(); window.addEventListener("mouseup", pointerUp);
  disposals.push(() => window.removeEventListener("mouseup", pointerUp));
  const key = (type: string, init: KeyboardEventInit) => canvas.dispatchEvent(new KeyboardEvent(type, { ...init, bubbles: true }));
  const mouse = (type: string, init: MouseEventInit) => canvas.dispatchEvent(new MouseEvent(type, { ...init, bubbles: true }));
  return { input, connection, up, pointerUp, key, mouse };
}
afterEach(() => { for (const dispose of disposals.splice(0).reverse()) dispose(); });

test("releases every held key once, preserves physical key identity, and skips keys already released", () => {
  const { input, key, up } = setup();
  key("keydown", { key: "Control", code: "ControlLeft", location: 1 });
  key("keydown", { key: "Control", code: "ControlRight", location: 2 });
  key("keydown", { key: "a", code: "KeyA" });
  key("keydown", { key: "a", code: "KeyA", repeat: true });
  key("keydown", { key: "Enter", code: "Enter" });
  key("keyup", { key: "Enter", code: "Enter" });
  up.mockClear();
  input.release();
  expect(up.mock.calls.map(([event]: [KeyboardEvent]) => [event.key, event.code, event.location])).toEqual([
    ["Control", "ControlLeft", 1], ["Control", "ControlRight", 2], ["a", "KeyA", 0],
  ]);
  input.release();
  expect(up).toHaveBeenCalledTimes(3);
});

test.each(["", "Unidentified"])("releases distinct legacy keyboard events with code %j", code => {
  const { input, key, up } = setup();
  key("keydown", { key: "a", code, keyCode: 65 });
  key("keydown", { key: "b", code, keyCode: 66 });
  key("keyup", { key: "a", code, keyCode: 65 });
  up.mockClear(); input.release();
  expect(up).toHaveBeenCalledOnce();
  expect(up.mock.calls[0][0]).toMatchObject({ key: "b", code, keyCode: 66 });
});

test("releases all mouse buttons through the window capture using the last pointer position", () => {
  const { input, mouse, pointerUp } = setup();
  mouse("mousedown", { buttons: 3, clientX: 10, clientY: 20 });
  mouse("mouseup", { buttons: 1, clientX: 11, clientY: 21 });
  pointerUp.mockClear();
  mouse("mousemove", { buttons: 1, clientX: 45, clientY: 67 });
  input.release();
  expect(pointerUp).toHaveBeenCalledOnce();
  expect(pointerUp.mock.calls[0][0]).toMatchObject({ buttons: 0, clientX: 45, clientY: 67 });
  input.release();
  expect(pointerUp).toHaveBeenCalledOnce();
});

test("a completed mouse release and motion without held buttons do not create an extra remote pointer event", () => {
  const { input, mouse, pointerUp } = setup();
  mouse("mousemove", { clientX: 10, clientY: 20 });
  mouse("mousedown", { buttons: 1 });
  mouse("mouseup", { buttons: 0 });
  pointerUp.mockClear(); input.release();
  expect(pointerUp).not.toHaveBeenCalled();
});

test("read-only input cannot create synthetic releases after permission is granted", () => {
  const { input, connection, key, mouse, up, pointerUp } = setup(true);
  key("keydown", { key: "Control", code: "ControlLeft" });
  mouse("mousedown", { buttons: 1 });
  connection.viewOnly = false;
  input.release();
  expect(up).not.toHaveBeenCalled(); expect(pointerUp).not.toHaveBeenCalled();
});

test("losing window focus releases input and still allows a later manual interaction", () => {
  const { key, mouse, up, pointerUp } = setup();
  key("keydown", { key: "a", code: "KeyA" }); mouse("mousedown", { buttons: 1 });
  window.dispatchEvent(new Event("blur"));
  expect(up).toHaveBeenCalledOnce(); expect(pointerUp).toHaveBeenCalledOnce();
  key("keydown", { key: "b", code: "KeyB" }); mouse("mousedown", { buttons: 2 });
  window.dispatchEvent(new Event("blur"));
  expect(up).toHaveBeenCalledTimes(2); expect(pointerUp).toHaveBeenCalledTimes(2);
  expect(up.mock.calls[1][0].code).toBe("KeyB");
});

test("disposing releases input and removes tracking so a retired viewer cannot react to later focus changes", () => {
  const { input, key, mouse, up, pointerUp } = setup();
  key("keydown", { key: "a", code: "KeyA" }); mouse("mousedown", { buttons: 1 });
  input.dispose();
  expect(up).toHaveBeenCalledOnce(); expect(pointerUp).toHaveBeenCalledOnce();
  key("keydown", { key: "b", code: "KeyB" }); mouse("mousedown", { buttons: 2 });
  mouse("mousemove", { clientX: 7, clientY: 9 });
  window.dispatchEvent(new Event("blur")); input.dispose();
  expect(up).toHaveBeenCalledOnce(); expect(pointerUp).toHaveBeenCalledOnce();
});
