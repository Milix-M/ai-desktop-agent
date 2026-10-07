"use client";

import { useCallback, useRef, useState } from "react";

export const SIDEBAR_MIN_WIDTH = 280;
export const SIDEBAR_MAX_WIDTH = 720;
const DEFAULT_WIDTH = 380;
const STORAGE_KEY = "sidebar-width";

function loadWidth(): number {
  if (typeof window === "undefined") return DEFAULT_WIDTH;
  const v = Number(window.localStorage.getItem(STORAGE_KEY));
  if (Number.isFinite(v) && v >= SIDEBAR_MIN_WIDTH && v <= SIDEBAR_MAX_WIDTH) {
    return v;
  }
  return DEFAULT_WIDTH;
}

/** 右サイドバーの幅をドラッグで変更する。設定はlocalStorageに保存。 */
export function useSidebarWidth() {
  const [width, setWidth] = useState<number>(loadWidth);
  const widthRef = useRef(width);
  widthRef.current = width;
  const dragRef = useRef<{ startX: number; startW: number } | null>(null);

  const onResizeStart = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    dragRef.current = { startX: e.clientX, startW: widthRef.current };
    const onMove = (ev: MouseEvent) => {
      if (!dragRef.current) return;
      const next = dragRef.current.startW + (dragRef.current.startX - ev.clientX);
      const clamped = Math.min(SIDEBAR_MAX_WIDTH, Math.max(SIDEBAR_MIN_WIDTH, next));
      widthRef.current = clamped;
      setWidth(clamped);
    };
    const onUp = () => {
      dragRef.current = null;
      try {
        window.localStorage.setItem(STORAGE_KEY, String(widthRef.current));
      } catch {
        // ignore
      }
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  }, []);

  return { width, onResizeStart };
}
