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
