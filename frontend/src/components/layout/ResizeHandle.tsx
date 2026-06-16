"use client";

import { useCallback, useRef, useEffect } from "react";

interface ResizeHandleProps {
  onResize: (delta: number) => void;
  direction: "left" | "right";
}

export default function ResizeHandle({ onResize, direction }: ResizeHandleProps) {
  const dragging = useRef(false);
  const startX = useRef(0);
  // Keep a ref to the latest onResize to avoid stale closure during drag
  const onResizeRef = useRef(onResize);
  const directionRef = useRef(direction);

  useEffect(() => {
    onResizeRef.current = onResize;
  }, [onResize]);

  useEffect(() => {
    directionRef.current = direction;
  }, [direction]);

  const handleMouseDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    dragging.current = true;
    startX.current = e.clientX;

    const handleMouseMove = (ev: MouseEvent) => {
      if (!dragging.current) return;
      const delta = ev.clientX - startX.current;
      startX.current = ev.clientX;
      // For left panel: dragging right = positive delta = wider
      // For right panel: dragging left = negative delta = wider
      onResizeRef.current(directionRef.current === "left" ? delta : -delta);
    };

    const handleMouseUp = () => {
      dragging.current = false;
      document.removeEventListener("mousemove", handleMouseMove);
      document.removeEventListener("mouseup", handleMouseUp);
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };

    document.addEventListener("mousemove", handleMouseMove);
    document.addEventListener("mouseup", handleMouseUp);
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
  }, []);

  return (
    <div
      className="resize-handle"
      onMouseDown={handleMouseDown}
    >
      <div className="resize-handle-line" />
    </div>
  );
}
