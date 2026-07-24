// Контекстное меню по правой кнопке. Позиционируется у курсора и
// подтягивается внутрь окна, если не помещается.

import { useEffect, useLayoutEffect, useRef, useState } from "react";

export interface MenuItem {
  /** Разделитель: остальные поля не нужны. */
  sep?: boolean;
  label?: string;
  hint?: string;
  icon?: string;
  danger?: boolean;
  disabled?: boolean;
  onClick?: () => void;
  /** Строка кнопок внутри пункта (роли, направления перемещения). */
  choices?: { label: string; active?: boolean; onClick: () => void }[];
}

export interface MenuPosition {
  x: number;
  y: number;
}

export default function ContextMenu({ at, items, onClose }: {
  at: MenuPosition;
  items: MenuItem[];
  onClose: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState(at);

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const { width, height } = el.getBoundingClientRect();
    setPos({
      x: Math.min(at.x, window.innerWidth - width - 8),
      y: Math.min(at.y, window.innerHeight - height - 8),
    });
  }, [at.x, at.y]);

  useEffect(() => {
    const close = (e: Event) => {
      if (e instanceof KeyboardEvent && e.key !== "Escape") return;
      if (e.type === "mousedown" && ref.current?.contains(e.target as Node)) return;
      onClose();
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", close);
    window.addEventListener("resize", close);
    // Меню «приклеено» к странице: при прокрутке проще закрыть, чем догонять.
    window.addEventListener("scroll", close, true);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", close);
      window.removeEventListener("resize", close);
      window.removeEventListener("scroll", close, true);
    };
  }, [onClose]);

  return (
    <div ref={ref} className="ctx-menu" style={{ left: pos.x, top: pos.y }}
         onContextMenu={(e) => e.preventDefault()}>
      {items.map((item, i) => {
        if (item.sep) return <div key={i} className="ctx-sep" />;
        if (item.choices) {
          return (
            <div key={i} className="ctx-choices">
              <span className="ctx-choices-label">{item.label}</span>
              <span className="row">
                {item.choices.map((c) => (
                  <button key={c.label}
                          className={`btn sm ${c.active ? "primary" : "ghost"}`}
                          onClick={() => { c.onClick(); onClose(); }}>
                    {c.label}
                  </button>
                ))}
              </span>
            </div>
          );
        }
        return (
          <button key={i} className={`ctx-item${item.danger ? " danger" : ""}`}
                  disabled={item.disabled}
                  onClick={() => { item.onClick?.(); onClose(); }}>
            <span className="ctx-icon">{item.icon ?? ""}</span>
            <span className="grow">{item.label}</span>
            {item.hint && <kbd className="ctx-hint">{item.hint}</kbd>}
          </button>
        );
      })}
    </div>
  );
}
