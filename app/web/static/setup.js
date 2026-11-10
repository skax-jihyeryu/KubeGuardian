/* 규칙 선택은 저장 전까지 브라우저에만 남는다. */
(function () {
  var form = document.getElementById('rule-selection');
  if (!form) return;
  var boxes = Array.from(form.querySelectorAll('input[name="enabled"]:not(:disabled)'));
  function update() {
    document.getElementById('enabled-count').textContent = boxes.filter(function (b) { return b.checked; }).length;
    var changed = boxes.filter(function (b) { return b.checked !== b.defaultChecked; }).length;
    document.getElementById('rule-dirty').textContent = changed ? '저장하지 않은 변경 ' + changed + '건' : '저장된 선택과 같습니다.';
    boxes.forEach(function (b) { b.closest('.mrow').classList.toggle('off', !b.checked); });
  }
  form.addEventListener('change', update);
  form.addEventListener('reset', function () { setTimeout(update, 0); });
  form.querySelectorAll('[data-rule-select]').forEach(function (button) {
    button.addEventListener('click', function () {
      boxes.forEach(function (b) { b.checked = button.dataset.ruleSelect === 'all'; });
      update();
    });
  });
})();
