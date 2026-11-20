// @vitest-environment node
import { renderToString } from "react-dom/server";
import { expect, test } from "vitest";
import RootLayout, { metadata } from "./layout";
import { useSidebarWidth } from "@/hooks/useSidebarWidth";
import { getVncWsUrl, getWsUrl } from "@/lib/api";

function Workspace() {
  const { width } = useSidebarWidth();
  return <aside style={{ width }}>操作画面</aside>;
}

test("the Japanese workspace can be rendered on the server without browser storage", () => {
  const html = renderToString(<RootLayout><Workspace /></RootLayout>);
  expect(html).toContain('lang="ja"');
  expect(html).toContain('<aside style="width:380px">操作画面</aside>');
  expect(metadata.title).toBe("AI Desktop Agent");
});

test("server rendering supplies local WebSocket defaults and encodes environment IDs", () => {
  expect(getWsUrl()).toBe("ws://localhost:8081/ws");
  expect(getVncWsUrl()).toBe("ws://localhost:8081/vnc/vm");
  expect(getVncWsUrl("作業/1")).toBe("ws://localhost:8081/vnc/%E4%BD%9C%E6%A5%AD%2F1");
});
