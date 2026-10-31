import { useEffect, useRef } from 'react';

export function useDialogKeyboard(close: () => void, blocked: boolean) {
  const current = useRef({ close, blocked });
  useEffect(() => { current.current = { close, blocked }; }, [close, blocked]);
  useEffect(() => {
    const previouslyFocused = document.activeElement as HTMLElement | null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    function keydown(event: KeyboardEvent) {
      if (event.key === 'Escape' && !current.current.blocked) { event.preventDefault(); current.current.close(); }
      if (event.key !== 'Tab') return;
      const controls = Array.from(document.querySelectorAll<HTMLElement>('[role="dialog"] button:not(:disabled), [role="dialog"] input:not(:disabled), [role="dialog"] select:not(:disabled), [role="dialog"] textarea:not(:disabled), [role="dialog"] a[href], [role="dialog"] summary, [role="dialog"] [tabindex="0"]')).filter(element => element.getClientRects().length > 0 && !element.classList.contains('sr-only'));
      if (!controls.length) { event.preventDefault(); return; }
      const first = controls[0]; const last = controls[controls.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }
    document.addEventListener('keydown', keydown);
    return () => { document.removeEventListener('keydown', keydown); document.body.style.overflow = previousOverflow; previouslyFocused?.focus(); };
  }, []);
}
