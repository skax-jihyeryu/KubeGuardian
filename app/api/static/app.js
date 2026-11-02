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
