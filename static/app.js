(() => {
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const postActivity = (event, details = '') => {
    const root = document.querySelector('[data-exam-session]');
    if (!root) return;
    const payload = JSON.stringify({ event, details });
    if (event === 'browser_exit' && navigator.sendBeacon) {
      navigator.sendBeacon(root.dataset.activityUrl, new Blob([payload], { type: 'application/json' }));
    } else {
      fetch(root.dataset.activityUrl, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: payload, keepalive: true }).catch(() => {});
    }
  };

  $$('[data-countdown]').forEach(el => {
    let seconds = Number(el.dataset.countdown || 0);
    const target = el.matches('.timer') ? el.querySelector('strong') : el;
    const render = () => {
      const h = Math.floor(seconds / 3600), m = Math.floor(seconds % 3600 / 60), s = seconds % 60;
      target.textContent = [h, m, s].map(n => String(n).padStart(2, '0')).join(':');
      if (seconds <= 300) target.style.color = 'var(--red)';
      seconds = Math.max(0, seconds - 1);
    };
    render(); setInterval(render, 1000);
  });

  const orientation = document.querySelector('[data-orientation]');
  if (orientation) {
    const slides = $$('[data-slide]', orientation), next = orientation.querySelector('[data-slide-next]'), prev = orientation.querySelector('[data-slide-prev]');
    const complete = orientation.querySelector('[data-slide-complete]'), number = orientation.querySelector('[data-slide-number]'), bar = orientation.querySelector('[data-slide-progress]');
    let index = 0;
    const show = value => {
      index = Math.max(0, Math.min(slides.length - 1, value));
      slides.forEach((slide, i) => slide.classList.toggle('active', i === index));
      number.textContent = index + 1; bar.style.width = `${((index + 1) / slides.length) * 100}%`; prev.disabled = index === 0;
      next.hidden = index === slides.length - 1; complete.hidden = index !== slides.length - 1;
    };
    next.addEventListener('click', () => show(index + 1)); prev.addEventListener('click', () => show(index - 1));
  }

  const startForm = document.querySelector('[data-start-exam]');
  if (startForm) startForm.addEventListener('submit', () => {
    const raw = [navigator.userAgent, navigator.language, screen.width, screen.height, screen.colorDepth, Intl.DateTimeFormat().resolvedOptions().timeZone].join('|');
    startForm.querySelector('[name=device_fingerprint]').value = raw;
  });

  const answerForm = document.querySelector('[data-answer-form]');
  if (answerForm) {
    const submit = answerForm.querySelector('[data-submit-answer]');
    $$('input[name=answer]', answerForm).forEach(input => input.addEventListener('change', () => { if (submit) submit.disabled = !answerForm.querySelector('input[name=answer]:checked'); }));
    answerForm.addEventListener('submit', e => {
      if (!answerForm.dataset.confirmed && !confirm('Lock this answer? You cannot return to this question.')) { e.preventDefault(); return; }
      answerForm.dataset.intentional = 'true'; if (submit) { submit.disabled = true; submit.textContent = 'Saving response…'; }
    });
  }

  const exam = document.querySelector('[data-exam-session]');
  if (exam) {
    const modal = document.querySelector('[data-security-modal]');
    let intentional = false, lastWarning = 0;
    document.addEventListener('submit', () => intentional = true);
    document.addEventListener('visibilitychange', () => { if (document.hidden) { postActivity('tab_hidden'); modal.hidden = false; } });
    document.addEventListener('fullscreenchange', () => { if (!document.fullscreenElement && Date.now() - lastWarning > 1000) { postActivity('fullscreen_exit'); modal.hidden = false; } });
    document.addEventListener('copy', e => { e.preventDefault(); postActivity('copy_attempt'); });
    document.addEventListener('contextmenu', e => { e.preventDefault(); postActivity('context_menu'); });
    window.addEventListener('beforeunload', () => { if (!intentional) postActivity('browser_exit'); });
    setInterval(() => postActivity('heartbeat'), 60000);
    const enterFullscreen = () => { lastWarning = Date.now(); document.documentElement.requestFullscreen?.().catch(() => {}); modal.hidden = true; };
    document.querySelector('[data-fullscreen]')?.addEventListener('click', enterFullscreen);
    document.querySelector('[data-return-exam]')?.addEventListener('click', enterFullscreen);
  }

  setTimeout(() => $$('.toast').forEach(t => { t.style.opacity = '0'; setTimeout(() => t.remove(), 250); }), 5500);
})();
