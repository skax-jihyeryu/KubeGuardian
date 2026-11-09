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

/* 기간 선택기. 채팅에서는 대상마다 확정한 값을 각 칩에 저장한다. */
function syncPeriodButtons(picker) {
  var mode = picker.querySelector('[data-period-mode]').value;
  picker.querySelectorAll('[data-period-quick]').forEach(function (button) {
    button.setAttribute('aria-pressed', String(button.dataset.periodQuick === mode));
  });
}

function chatPeriodChip(picker) {
  var target = picker.querySelector('[name="chat_period_target"]:checked')?.value;
  return target && picker.closest('#chat-panel').querySelector('[data-chat-period-chip="' + target + '"]');
}

function loadChatPeriod(picker) {
  var chip = chatPeriodChip(picker);
  if (!chip) return;
  for (var [field, control] of [['period', 'mode'], ['from', 'start'], ['to', 'end']]) {
    picker.querySelector('[data-period-' + control + ']').value =
      chip.querySelector('[data-saved-period="' + field + '"]').value;
  }
  picker.querySelector('[data-period-end]').setCustomValidity('');
  syncPeriodButtons(picker);
}

document.addEventListener('change', function (e) {
  var picker = e.target.closest('[data-period-picker]');
  if (!picker) return;
  if (e.target.name === 'chat_period_target') {
    loadChatPeriod(picker);
    return;
  }
  var mode = picker.querySelector('[data-period-mode]');
  var start = picker.querySelector('[data-period-start]');
  var end = picker.querySelector('[data-period-end]');
  if (e.target === mode && mode.value !== 'custom') {
    start.value = '';
    end.value = '';
  }
  if (e.target === start || e.target === end) mode.value = 'custom';
  end.setCustomValidity('');
  syncPeriodButtons(picker);
});

document.addEventListener('click', function (e) {
  var summary = e.target.closest('[data-period-picker] > summary');
  if (summary && !summary.parentElement.open) loadChatPeriod(summary.parentElement);
  var quick = e.target.closest('[data-period-quick]');
  if (quick) {
    var mode = quick.closest('[data-period-picker]').querySelector('[data-period-mode]');
    mode.value = quick.dataset.periodQuick;
    mode.dispatchEvent(new Event('change', {bubbles: true}));
  }
  var done = e.target.closest('[data-period-done]');
  if (done) {
    var picker = done.closest('[data-period-picker]');
    var chip = chatPeriodChip(picker);
    var mode = picker.querySelector('[data-period-mode]').value;
    var start = picker.querySelector('[data-period-start]').value;
    var end = picker.querySelector('[data-period-end]');
    if (mode === 'custom' && (!start || !end.value || start > end.value)) {
      end.setCustomValidity('시작일과 종료일을 순서대로 선택해 주세요.');
      end.reportValidity();
      return;
    }
    if (chip && mode) {
      chip.querySelector('[data-saved-period="period"]').value = mode;
      chip.querySelector('[data-saved-period="from"]').value = start;
      chip.querySelector('[data-saved-period="to"]').value = end.value;
      var range = mode === 'custom' ? start + ' ~ ' + end.value :
        picker.querySelector('[data-period-quick][aria-pressed="true"]').textContent;
      chip.querySelector('[data-period-label]').textContent =
        chip.dataset.periodTargetLabel + ' · ' + range;
      chip.hidden = false;
    }
    picker.open = false;
    picker.querySelector('summary').focus();
  }
  document.querySelectorAll('[data-period-picker][open]').forEach(function (picker) {
    if (!picker.contains(e.target)) picker.open = false;
  });
});

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
    var view = location.pathname.split('/')[2];
    if (view === 'checks' || view === 'issues') params.set('view', view);
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
  function toggleMentions(open) {
    var picker = document.getElementById('chat-mention-picker');
    var button = document.querySelector('[data-chat-mention-toggle]');
    picker.hidden = !open;
    button.setAttribute('aria-expanded', String(open));
    var input = picker.querySelector('input');
    input.setAttribute('aria-expanded', String(open));
    if (open) {
      input.focus();
      htmx.trigger(input, 'chat-mention-open');
    } else {
      input.removeAttribute('aria-activedescendant');
      button.focus();
    }
  }
  function addRecord(option) {
    var kind = option.dataset.chatPick;
    var id = option.dataset.recordId;
    var records = document.querySelector('[data-chat-records]');
    var exists = Array.from(records.querySelectorAll('input')).some(
      input => input.name === kind && input.value === id);
    if (exists) return;
    var chip = document.createElement('span');
    chip.className = 'ctx';
    chip.dataset.chatRecord = '';
    var label = document.createElement('span');
    label.textContent = (kind === 'issue' ? '이슈 #' : '점검 #') + id;
    var remove = document.createElement('button');
    remove.className = 'plain';
    remove.type = 'button';
    remove.dataset.chatRemove = '';
    remove.setAttribute('aria-label', label.textContent + ' 제외');
    remove.textContent = '×';
    var input = document.createElement('input');
    input.type = 'hidden';
    input.name = kind;
    input.value = id;
    input.setAttribute('form', 'chat-question');
    chip.append(label, remove, input);
    records.append(chip);
  }
  function closeHistory() {
    document.getElementById('chat-history').replaceChildren();
    document.querySelector('[data-chat-history-open]').focus();
  }
  document.addEventListener('click', function (e) {
    if (e.target.closest('[data-chat-mention-toggle]')) {
      toggleMentions(document.getElementById('chat-mention-picker').hidden);
    }
    var option = e.target.closest('[data-chat-pick]');
    if (option) {
      addRecord(option);
      toggleMentions(false);
    }
    if (e.target.closest('[data-chat-history-close]')) closeHistory();
    var remove = e.target.closest('[data-chat-remove]');
    if (remove) {
      remove.closest('[data-chat-record]').remove();
      document.querySelector('[data-chat-mention-toggle]').focus();
    }
    var removeFilter = e.target.closest('[data-chat-remove-filter]');
    if (removeFilter) {
      var form = document.getElementById('chat-question');
      form.elements.filters.value = '';
      form.elements.view.value = '';
      removeFilter.closest('[data-chat-page-filter]').remove();
      document.querySelector('[data-chat-mention-toggle]').focus();
    }
    var removePeriod = e.target.closest('[data-chat-remove-period]');
    if (removePeriod) {
      var chip = removePeriod.closest('[data-chat-period-chip]');
      chip.querySelectorAll('[data-saved-period]').forEach(input => { input.value = ''; });
      chip.hidden = true;
      var picker = document.querySelector('#chat-panel [data-period-picker]');
      if (chatPeriodChip(picker) === chip) loadChatPeriod(picker);
      picker.querySelector('summary').focus();
    }
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
  document.addEventListener('input', function (e) {
    if (!e.target.matches('[data-chat-mention]')) return;
    e.target.removeAttribute('aria-activedescendant');
    document.getElementById('chat-mention-results').replaceChildren();
  });
  document.addEventListener('keydown', function (e) {
    if (e.isComposing) return;
    if (e.target.matches('[data-chat-mention]') &&
        ['ArrowDown', 'ArrowUp', 'Enter'].includes(e.key)) {
      e.preventDefault();
      var options = Array.from(document.querySelectorAll('#chat-mention-results [data-chat-pick]'));
      if (!options.length) return;
      var activeId = e.target.getAttribute('aria-activedescendant');
      var current = options.findIndex(option => option.id === activeId);
      if (e.key === 'Enter') {
        options[Math.max(current, 0)].click();
        return;
      }
      var offset = e.key === 'ArrowDown' ? 1 : -1;
      var index = current < 0 ? (offset > 0 ? 0 : options.length - 1) :
        (current + offset + options.length) % options.length;
      var next = options[index];
      options.forEach(option => option.setAttribute('aria-selected', String(option === next)));
      e.target.setAttribute('aria-activedescendant', next.id);
      next.scrollIntoView({block: 'nearest'});
      return;
    }
    if (e.key !== 'Escape') return;
    var period = e.target.closest('[data-period-picker][open]');
    if (period) {
      period.open = false;
      period.querySelector('summary').focus();
      return;
    }
    if (!document.getElementById('chat-panel')) return;
    if (document.querySelector('#chat-history .chat-history')) closeHistory();
    else if (!document.getElementById('chat-mention-picker').hidden) toggleMentions(false);
    else closeChat();
  });
  document.body.addEventListener('htmx:beforeSwap', function (e) {
    if (e.detail.target.id !== 'chat-mention-results') return;
    var input = document.querySelector('[data-chat-mention]');
    if (input && (e.detail.requestConfig.parameters.q || '') !== input.value) {
      e.detail.shouldSwap = false;
    }
  });
  document.body.addEventListener('htmx:afterSwap', function (e) {
    if (e.detail.target.id === 'chat-mention-results') {
      document.querySelector('[data-chat-mention]')?.removeAttribute('aria-activedescendant');
    }
    if (e.detail.target.id === 'chat-history') {
      e.detail.target.querySelector('[data-chat-history-close]')?.focus();
    }
  });
  document.body.addEventListener('htmx:beforeRequest', function (e) {
    if (e.target.matches('[data-chat-resume]')) {
      var pending = document.getElementById('chat-question')?.chatPending;
      if (pending) { e.preventDefault(); return; }
    }
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
    document.querySelector('#chat-panel [data-period-picker]').open = false;
    document.getElementById('chat-mention-picker').hidden = true;
    document.querySelector('[data-chat-mention-toggle]').setAttribute('aria-expanded', 'false');
    var mention = document.querySelector('[data-chat-mention]');
    mention.setAttribute('aria-expanded', 'false');
    mention.removeAttribute('aria-activedescendant');
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

/* 빠른 기간 선택과 시작~끝 날짜는 둘 중 하나만 쓴다: 한쪽을 바꾸면 다른 쪽을 비운다. */
document.addEventListener('change', function (event) {
  var field = event.target;
  if (!field.form) return;
  if (field.matches('select[name="period"], select[name="days"]')) {
    field.form.querySelectorAll('input[name="from"], input[name="to"]').forEach(function (input) {
      input.value = '';
    });
  } else if (field.matches('input[name="from"], input[name="to"]')) {
    var quick = field.form.querySelector('select[name="period"], select[name="days"]');
    if (quick) quick.selectedIndex = 0;
  }
});
