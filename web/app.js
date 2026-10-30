// Solanum Catalog: the home page's search and category filter. No library. SPDX-License-Identifier: GPL-3.0-or-later
(function () {
  'use strict';
  var input = document.getElementById('q');
  var grid = document.getElementById('grid');
  if (!input || !grid) return;
  var cards = Array.prototype.slice.call(grid.children);
  var chips = Array.prototype.slice.call(document.querySelectorAll('.chip'));
  var count = document.getElementById('count');
  var none = document.getElementById('none');
  var category = '';

  function apply() {
    var words = input.value.toLowerCase().split(/\s+/).filter(Boolean);
    var shown = 0;
    cards.forEach(function (card) {
      var text = card.getAttribute('data-search');
      var show = (!category || card.getAttribute('data-category') === category) &&
        words.every(function (word) { return text.indexOf(word) !== -1; });
      card.hidden = !show;
      if (show) shown++;
    });
    chips.forEach(function (chip) {
      chip.setAttribute('aria-pressed', chip.getAttribute('data-category') === category ? 'true' : 'false');
    });
    count.textContent = shown === cards.length ? cards.length + ' apps' : 'Showing ' + shown + ' of ' + cards.length + ' apps';
    none.hidden = shown !== 0;
    var query = [];
    if (input.value.trim()) query.push('q=' + encodeURIComponent(input.value.trim()));
    if (category) query.push('c=' + encodeURIComponent(category));
    try { history.replaceState(null, '', location.pathname + (query.length ? '?' + query.join('&') : '')); } catch (e) { /* a file: page */ }
  }

  chips.forEach(function (chip) {
    chip.addEventListener('click', function () { category = chip.getAttribute('data-category'); apply(); });
  });
  input.addEventListener('input', apply);
  document.addEventListener('keydown', function (event) {
    if (event.key === '/' && document.activeElement !== input && !event.ctrlKey && !event.metaKey && !event.altKey) {
      event.preventDefault();
      input.focus();
    }
  });
  var params = new URLSearchParams(location.search);
  input.value = params.get('q') || '';
  var wanted = params.get('c') || '';
  category = chips.some(function (chip) { return chip.getAttribute('data-category') === wanted; }) ? wanted : '';
  apply();
})();
