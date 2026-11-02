/* 권장 조치는 텍스트로만 복사한다. 실행하지 않는다. */
(function () {
  document.addEventListener('click', function (event) {
    var button = event.target.closest('[data-copy-action]');
    if (!button || !navigator.clipboard) return;
    navigator.clipboard.writeText(button.dataset.copyAction).then(function () {
      button.textContent = '복사했습니다';
      setTimeout(function () { button.textContent = '복사'; }, 2000);
    }, function () { button.textContent = '복사 실패'; });
  });
})();
