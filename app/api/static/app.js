/* 서랍 닫기(Esc)·링크 복사, 폴링으로 바뀌는 점검 과정의 펼침 상태 유지. 로그·LLM 문자열은 다루지 않는다. */
(function () {
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape") return;
    var close = document.getElementById("drawer-close");
    if (close) close.click();
  });

  document.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-copy-path]");
    if (!btn) return;
    var url = location.origin + btn.getAttribute("data-copy-path");
    var done = function () {
      var note = document.getElementById("copied");
      if (!note) return;
      note.textContent = "복사했습니다";
      setTimeout(function () { note.textContent = ""; }, 2000);
    };
    if (navigator.clipboard) navigator.clipboard.writeText(url).then(done, function () {});
  });

  var opened = null;
  document.body.addEventListener("htmx:beforeSwap", function (e) {
    var t = e.detail.target;
    if (!t || t.id !== "steps") return;
    opened = {};
    t.querySelectorAll("details[data-k]").forEach(function (d) { opened[d.dataset.k] = d.open; });
  });
  document.body.addEventListener("htmx:afterSettle", function () {
    if (!opened) return;
    var steps = document.getElementById("steps");
    if (steps) steps.querySelectorAll("details[data-k]").forEach(function (d) {
      if (d.dataset.k in opened) d.open = opened[d.dataset.k];
    });
    opened = null;
  });
})();

/* 근거 전체 표는 접혀 있다. 바로가기(#s-ev)나 #evidence 주소로 들어오면 펼친다. */
(function () {
  function openEvidence() {
    if (location.hash !== "#s-ev" && location.hash !== "#evidence") return;
    var d = document.getElementById("evidence");
    if (!d) return;
    d.open = true;
    document.getElementById("s-ev").scrollIntoView();
  }
  window.addEventListener("hashchange", openEvidence);
  document.addEventListener("click", function (e) {
    var a = e.target.closest('.jump a[href="#s-ev"]');
    if (a) setTimeout(openEvidence, 0);
  });
  openEvidence();
})();

/* 질문 패널은 페이지 이동 뒤에도 열린 상태를 유지한다. */
(function () {
  function openChat() {
    var btn = document.querySelector('[data-chat-toggle]');
    if (!btn) return;
    var path = location.pathname.match(/\/(issues|checks)\/(\d+)$/);
    var url = btn.dataset.chatUrl;
    var params = new URLSearchParams();
    if (path) params.set(path[1] === 'issues' ? 'issue' : 'run', path[2]);
    if (location.search) params.set('filters', location.search.slice(1));
    url += '?' + params.toString();
    htmx.ajax('GET', url, {target: '#chat-root', swap: 'innerHTML'});
    btn.setAttribute('aria-expanded', 'true');
    sessionStorage.setItem('kg-chat-open', '1');
  }
  function abortChat() {
    var reset = document.querySelector('[data-chat-new]');
    if (reset) htmx.trigger(reset, 'htmx:abort');
    var form = document.querySelector('#chat-panel form');
    if (form && form.chatPending) {
      form.chatPending.cancelled = true;
      htmx.trigger(form, 'htmx:abort');
    }
  }
  function closeChat() {
    abortChat();
    document.getElementById('chat-root').replaceChildren();
    var btn = document.querySelector('[data-chat-toggle]');
    if (btn) btn.setAttribute('aria-expanded', 'false');
    sessionStorage.removeItem('kg-chat-open');
  }
  document.addEventListener('click', function (e) {
    if (e.target.closest('[data-chat-close]')) closeChat();
    if (e.target.closest('[data-chat-toggle]')) {
      if (document.getElementById('chat-panel')) closeChat(); else openChat();
    }
    var example = e.target.closest('[data-question]');
    if (example) {
      var input = document.querySelector('#chat-panel textarea');
      if (input.readOnly) return;
      input.value = example.dataset.question;
      input.focus();
      input.form.requestSubmit();
    }
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && document.getElementById('chat-panel')) closeChat();
  });
  document.body.addEventListener('htmx:beforeRequest', function (e) {
    if (e.target.matches('[data-chat-new]')) {
      var currentForm = document.querySelector('#chat-panel form');
      if (currentForm && currentForm.chatPending) { e.preventDefault(); return; }
      var error = document.querySelector('[data-chat-new-error]');
      e.target.chatResetError = error;
      if (error) { error.hidden = true; error.textContent = ''; }
    }
    if (e.detail.target && e.detail.target.id === 'chat-root' &&
        !e.target.matches('[data-chat-new]')) abortChat();
    var form = e.target;
    if (!form.matches('#chat-panel form')) return;
    if (form.chatPending) { e.preventDefault(); return; }
    var input = form.querySelector('textarea');
    var messages = document.getElementById('chat-messages');
    var question = input.value.trim();
    var turn = document.createElement('div');
    turn.className = 'chat-pending';
    var bubble = document.createElement('div');
    bubble.className = 'q';
    bubble.textContent = question;
    var status = document.createElement('div');
    status.className = 'chat-loading';
    status.setAttribute('role', 'status');
    status.textContent = '저장된 이력을 확인해 답변을 작성하고 있습니다…';
    turn.append(bubble, status);
    var empty = messages.querySelector('#chat-empty');
    if (empty) empty.remove();
    messages.append(turn);
    var button = form.querySelector('button[type="submit"]');
    form.chatPending = {turn: turn, status: status, messages: messages,
      input: input, button: button, question: question};
    form.setAttribute('aria-busy', 'true');
    input.readOnly = true;
    input.value = '';
    button.textContent = '답변 생성 중';
    messages.scrollTop = messages.scrollHeight;
  });
  document.body.addEventListener('htmx:afterRequest', function (e) {
    if (e.target.matches('[data-chat-new]') && !e.detail.successful) {
      var error = e.target.chatResetError;
      if (error && error.isConnected) {
        error.textContent = '새 채팅을 시작하지 못했습니다. 다시 시도해 주세요.';
        error.hidden = false;
      }
    }
    var form = e.target;
    if (!form.matches('#chat-panel form') || !form.chatPending) return;
    var pending = form.chatPending;
    var failed = !e.detail.successful ||
      e.detail.xhr.responseText.indexOf('role="alert"') >= 0;
    if (e.detail.successful || pending.cancelled) {
      pending.turn.remove();
    } else {
      pending.turn.className = 'chat-failed';
      pending.status.className = 'tipblock';
      pending.status.setAttribute('role', 'alert');
      pending.status.textContent = '답변을 받지 못했습니다. 연결 상태를 확인한 뒤 다시 보내 주세요.';
    }
    pending.input.readOnly = false;
    pending.input.value = failed ? pending.question : '';
    pending.button.textContent = '보내기';
    form.removeAttribute('aria-busy');
    delete form.chatPending;
    pending.messages.scrollTop = pending.messages.scrollHeight;
    if (form.isConnected) pending.input.focus();
  });
  if (sessionStorage.getItem('kg-chat-open')) openChat();
})();

/* 점검 완료 뒤 GNB 갱신과 대기 시간 해제를 함께 처리한다. */
(function () {
  function cooldown() {
    var button = document.querySelector('[data-check-wait]');
    if (!button || button.dataset.timer) return;
    button.dataset.timer = '1';
    setTimeout(function () {
      button.disabled = false;
      button.classList.remove('btn-dis');
      button.classList.add('btn-primary');
      button.title = '지금 점검 실행';
    }, Number(button.dataset.checkWait) * 1000);
  }
  document.body.addEventListener('htmx:afterSwap', function () {
    cooldown();
    var button = document.querySelector('[data-chat-toggle]');
    if (button) button.setAttribute('aria-expanded', String(!!document.getElementById('chat-panel')));
  });
  cooldown();
})();

/* 서버가 계산한 경과 시간에 단조 시계의 차이만 더한다. 조각 교체에도 타이머는 하나다. */
(function () {
  var current = null;
  var started = 0;
  var elapsed = 0;
  var interval = null;

  function stop() {
    if (interval !== null) clearInterval(interval);
    interval = null;
  }
  function tick() {
    if (!current || !current.isConnected) {
      sync();
      return;
    }
    var seconds = elapsed + Math.floor(Math.max(0, performance.now() - started) / 1000);
    current.textContent = seconds < 60 ? seconds + '초' :
      Math.floor(seconds / 60) + '분 ' + (seconds % 60) + '초';
  }
  function sync() {
    var node = document.querySelector('[data-check-elapsed]');
    if (node !== current) {
      current = node;
      started = performance.now();
      elapsed = node ? Number(node.dataset.checkElapsed) : 0;
    }
    if (!current) {
      stop();
      return;
    }
    if (interval === null) interval = setInterval(tick, 1000);
    tick();
  }
  document.body.addEventListener('htmx:afterSwap', sync);
  window.addEventListener('pagehide', stop);
  window.addEventListener('pageshow', sync);
  sync();
})();

/* 기본 폼 전송을 유지하면서 응답을 기다리는 동안 중복 제출을 막는다. */
(function () {
  document.addEventListener('submit', function (event) {
    var form = event.target;
    if (!form.matches('#gnb form[method="post"]')) return;
    if (form.dataset.submitting) {
      event.preventDefault();
      return;
    }
    form.dataset.submitting = '1';
    var button = form.querySelector('button[type="submit"]');
    if (!button) return;
    button.disabled = true;
    button.setAttribute('aria-busy', 'true');
    button.setAttribute('aria-label', '점검 시작 중…');
    button.title = '점검 시작 중…';
    var label = button.querySelector('.lbl');
    if (label) label.textContent = '점검 시작 중…';
    var icon = button.querySelector('.ic');
    if (icon) icon.classList.add('loading-spin');
  });
})();

/* 뒤로 가기로 복원된 폼은 서버의 현재 잠금·대기 상태를 다시 읽는다. */
window.addEventListener('pageshow', function (event) {
  if (!event.persisted) return;
  var form = document.querySelector('#gnb form[method="post"]');
  if (!form) return;
  var url = new URL(form.action, location.href);
  if (url.origin !== location.origin || !/\/checks$/.test(url.pathname)) return;
  htmx.ajax('GET', url.pathname.replace(/\/checks$/, '/shell'), {
    target: '#gnb', swap: 'outerHTML'
  });
});

/* 점검 이력은 기간 메뉴에서 날짜 입력을 열고, 이슈 이력은 두 필터를 따로 둔다. */
document.addEventListener('change', function (event) {
  var field = event.target;
  if (!field.form) return;
  if (field.matches('[data-check-period]')) {
    var range = field.form.querySelector('[data-check-date-range]');
    range.hidden = range.disabled = field.value !== 'custom';
  } else if (field.matches('select[name="days"]')) {
    field.form.querySelectorAll('input[name="from"], input[name="to"]').forEach(function (input) {
      input.value = '';
    });
  } else if (field.matches('input[name="from"], input[name="to"]')) {
    var quick = field.form.querySelector('select[name="days"]');
    if (quick) quick.selectedIndex = 0;
  }
});
