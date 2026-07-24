// Оболочка модального окна: затемнение, шапка с крестиком, Esc на закрытие.

import { useEffect, type ReactNode } from "react";

export default function Modal({ title, wide, onClose, foot, children }: {
  title: ReactNode;
  wide?: boolean;
  onClose: () => void;
  foot?: ReactNode;
  children: ReactNode;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="overlay" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className={`modal${wide ? " wide" : ""}`}>
        <div className="modal-head">
          <h2>{title}</h2>
          <button className="btn ghost sm" onClick={onClose} title="Закрыть">✕</button>
        </div>
        <div className="modal-body">{children}</div>
        {foot && <div className="modal-foot">{foot}</div>}
      </div>
    </div>
  );
}

/** Модалка с одним текстовым полем — замена window.prompt. */
export function PromptModal({ title, label, initial = "", confirmLabel = "Применить",
                             onSubmit, onClose }: {
  title: string;
  label: string;
  initial?: string;
  confirmLabel?: string;
  onSubmit: (value: string) => void;
  onClose: () => void;
}) {
  let value = initial;
  return (
    <Modal title={title} onClose={onClose} foot={
      <>
        <button className="btn ghost" onClick={onClose}>Отмена</button>
        <button className="btn primary" onClick={() => {
          if (value.trim()) { onSubmit(value.trim()); onClose(); }
        }}>{confirmLabel}</button>
      </>
    }>
      <label className="field">{label}
        <input className="input" autoFocus defaultValue={initial}
               onChange={(e) => { value = e.target.value; }}
               onKeyDown={(e) => {
                 if (e.key === "Enter" && value.trim()) { onSubmit(value.trim()); onClose(); }
               }} />
      </label>
    </Modal>
  );
}

/** Подтверждение опасного действия — замена window.confirm. */
export function ConfirmModal({ title, message, confirmLabel = "Удалить", onConfirm, onClose }: {
  title: string;
  message: ReactNode;
  confirmLabel?: string;
  onConfirm: () => void;
  onClose: () => void;
}) {
  return (
    <Modal title={title} onClose={onClose} foot={
      <>
        <button className="btn ghost" onClick={onClose}>Отмена</button>
        <button className="btn danger" autoFocus
                onClick={() => { onConfirm(); onClose(); }}>{confirmLabel}</button>
      </>
    }>
      <div>{message}</div>
      <p className="muted small" style={{ marginBottom: 0 }}>
        Действие можно отменить кнопкой ↶ или Ctrl+Z.
      </p>
    </Modal>
  );
}
