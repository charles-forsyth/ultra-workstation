// Copy-to-clipboard for install command blocks. No dependencies.
(function () {
  'use strict';
  document.querySelectorAll('.copy-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var target = document.getElementById(btn.getAttribute('data-target'));
      if (!target) {
        return;
      }
      var lines = target.innerText.split('\n').map(function (line) {
        return line.replace(/^\$ /, '');
      });
      var text = lines
        .filter(function (line) {
          return line.trim() !== '' && line.trim().charAt(0) !== '#';
        })
        .map(function (line) {
          return line.replace(/\s+#.*$/, '');
        })
        .join('\n');
      var done = function () {
        var old = btn.textContent;
        btn.textContent = 'Copied';
        setTimeout(function () {
          btn.textContent = old;
        }, 1500);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done, function () {});
      }
    });
  });
})();
