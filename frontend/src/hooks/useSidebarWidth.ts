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

function clampWidth(v: number): number {
  return Math.min(SIDEBAR_MAX_WIDTH, Math.max(SIDEBAR_MIN_WIDTH, v));
}

/** 右サイドバーの幅をドラッグで変更する。設定はlocalStorageに保存。 */
export function useSidebarWidth() {
  const [width, setWidth] = useState<number>(loadWidth);
  const dragRef = useRef<{ startX: number; startW: number } | null>(null);
  const widthRef = useRef(width);
  widthRef.current = width;

  const onResizeStart = useCallback((e: React.PointerEvent<HTMLElement>) => {
    const el = e.currentTarget;
    dragRef.current = { startX: e.clientX, startW: widthRef.current };
    try {
      el.setPointerCapture?.(e.pointerId);
    } catch {
      // ignore (jsdom等)
    }
  }, []);

  const onResizeMove = useCallback((e: React.PointerEvent<HTMLElement>) => {
    if (!dragRef.current) return;
    // 右パネルなので左へ動かすと広がる
    const next = dragRef.current.startW + (dragRef.current.startX - e.clientX);
    const clamped = clampWidth(next);
    widthRef.current = clamped;
    setWidth(clamped);
  }, []);

  const onResizeEnd = useCallback(() => {
    if (!dragRef.current) return;
    dragRef.current = null;
    try {
      window.localStorage.setItem(STORAGE_KEY, String(widthRef.current));
    } catch {
      // ignore
    }
  }, []);

  return { width, onResizeStart, onResizeMove, onResizeEnd };
}
