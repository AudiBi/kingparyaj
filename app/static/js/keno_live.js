/* app/static/js/keno_live.js
 * Keno partagé — affichage commun (panel agent, écran de salle, écran du guichet).
 *
 * Le navigateur n'invente rien : les 20 numéros, leur ordre et les gains
 * viennent du serveur. L'animation dévoile, au rythme de l'horloge du
 * serveur, des boules déjà tirées : un écran rechargé ou reconnecté reprend
 * exactement au même point que les autres.
 */
(function (global) {
  'use strict';

  var TOTAL = 80, DRAWN = 20;
  var TENS = ['#f2c300', '#f57c00', '#e53935', '#d81b60', '#8e24aa', '#1e6fd9', '#2e9d4a', '#7cb342'];
  var STATUS = { pending: 'Paris ouverts', completed: 'Tirage effectué', cancelled: 'Annulé' };

  function colorOf(n) { return TENS[Math.floor((n - 1) / 10)]; }
  function parseUtc(iso) { return iso ? Date.parse(iso) : null; }
  function ServerClock() { this.offset = 0; }
  ServerClock.prototype.sync = function (iso) { var s = parseUtc(iso); if (s) this.offset = s - Date.now(); };
  ServerClock.prototype.now = function () { return Date.now() + this.offset; };

  function countdown(ms) {
    if (ms == null || ms <= 0) return '00:00';
    var s = Math.ceil(ms / 1000), m = Math.floor(s / 60);
    s = s % 60;
    return (m < 10 ? '0' : '') + m + ':' + (s < 10 ? '0' : '') + s;
  }
  function money(x) {
    if (x == null || isNaN(x)) return '—';
    return Number(x).toLocaleString('fr-FR', { maximumFractionDigits: 2 }) + ' HTG';
  }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function ballHtml(n, extra) {
    return '<span class="kn-ball' + (extra ? ' ' + extra : '') + '" style="--c:' + colorOf(n) + '">' + n + '</span>';
  }

  /* Où en est le tirage, d'après l'heure du serveur. */
  function progress(draw, now) {
    if (!draw) return { phase: 'none', revealed: 0 };
    if (draw.status === 'cancelled') return { phase: 'cancelled', revealed: 0 };
    var order = draw.draw_order || [];
    if (draw.status !== 'completed' || !order.length) {
      var closes = parseUtc(draw.betting_closes_at);
      return { phase: now < closes ? 'open' : 'closed', revealed: 0 };
    }
    var t = now - parseUtc(draw.drawn_at);
    var intro = draw.intro_ms || 0, step = draw.ball_interval_ms || 1500;
    if (t < intro) return { phase: 'intro', revealed: 0 };
    var revealed = Math.min(order.length, Math.floor((t - intro) / step) + 1);
    var done = (t - intro) >= order.length * step;
    return { phase: done ? 'done' : 'drawing', revealed: revealed };
  }

  function hitsOf(picks, order, revealed) {
    var set = {}, hits = 0;
    (order || []).slice(0, revealed).forEach(function (n) { set[n] = true; });
    (picks || []).forEach(function (n) { if (set[n]) hits++; });
    return hits;
  }

  function connectLive(onMessage, onStatus, channel) {
    var ws = null, attempts = 0, closed = false;
    function open() {
      var proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
      try { ws = new WebSocket(proto + location.host + '/ws/draws/' + (channel || 'all')); } catch (e) { return retry(); }
      ws.onopen = function () { attempts = 0; onStatus && onStatus(true); };
      ws.onmessage = function (ev) {
        try {
          var msg = JSON.parse(ev.data);
          if (msg && (msg.type === 'keno_round' || msg.type === 'kn_screen' || msg.type === 'keno_draw')) onMessage(msg.data || msg, msg.type, msg.event);
        } catch (e) { /* message d'un autre jeu */ }
      };
      ws.onclose = function () { onStatus && onStatus(false); if (!closed) retry(); };
      ws.onerror = function () { try { ws.close(); } catch (e) {} };
    }
    function retry() { attempts++; setTimeout(open, Math.min(1000 * attempts, 15000)); }
    open();
    return { close: function () { closed = true; if (ws) ws.close(); } };
  }

  /* ------------------------------------------------------------------
   * Tableau : sphère, grande boule, 20 cases de sortie, grille 1-80.
   * ------------------------------------------------------------------ */
  function Board(root, options) {
    this.root = root;
    this.opts = options || {};
    this.clock = this.opts.clock || new ServerClock();
    this.draw = null;
    this.picks = [];
    this._shown = -1;
    this._key = '';
    this.reduced = global.matchMedia && global.matchMedia('(prefers-reduced-motion: reduce)').matches;
    this._build();
  }

  Board.prototype._build = function () {
    var slots = '', grid = '', drum = '';
    for (var i = 1; i <= DRAWN; i++) slots += '<div class="kn-slot"><span class="kn-slot-no">' + i + '</span></div>';
    for (var n = 1; n <= TOTAL; n++) grid += '<span class="kn-cell" data-n="' + n + '" style="--c:' + colorOf(n) + '">' + n + '</span>';
    for (var k = 0; k < 22; k++) {
      var a = k * 33 * Math.PI / 180, r = 18 + (k % 4) * 11;
      drum += '<span class="kn-drum-ball" style="--c:' + colorOf(1 + (k * 7) % 80) + ';left:' + (61 + r * Math.cos(a)).toFixed(1) +
        'px;top:' + (61 + r * Math.sin(a)).toFixed(1) + 'px"></span>';
    }
    this.root.innerHTML =
      '<div class="kn-board">' +
      ' <div class="kn-stage">' +
      '  <div class="kn-drum" aria-hidden="true"><div class="kn-drum-balls">' + drum + '</div></div>' +
      '  <div class="kn-current"><div class="kn-current-ball" aria-live="polite"></div>' +
      '   <div><div class="kn-count">0 / 20</div><div class="kn-phase"></div></div></div>' +
      '  <div class="kn-hits"><span>Trouvés</span><b class="kn-hits-val">0</b></div>' +
      ' </div>' +
      ' <div class="kn-slots">' + slots + '</div>' +
      ' <div class="kn-grid">' + grid + '</div>' +
      '</div>';
    this.$ = {
      current: this.root.querySelector('.kn-current-ball'),
      count: this.root.querySelector('.kn-count'),
      phase: this.root.querySelector('.kn-phase'),
      hits: this.root.querySelector('.kn-hits'),
      hitsVal: this.root.querySelector('.kn-hits-val'),
      slots: Array.prototype.slice.call(this.root.querySelectorAll('.kn-slot')),
      cells: Array.prototype.slice.call(this.root.querySelectorAll('.kn-cell'))
    };
  };

  Board.prototype.setDraw = function (draw) {
    var key = draw ? draw.draw_id + ':' + draw.status : '';
    if (key !== this._key) { this._key = key; this._shown = -1; }
    this.draw = draw;
    this.render();
  };

  Board.prototype.setPicks = function (picks) {
    var key = (picks || []).join(',');
    if (key === this._picksKey) return;
    this._picksKey = key;
    this.picks = (picks || []).slice();
    this._shown = -1;
    this.render();
  };

  Board.prototype.render = function () {
    var d = this.draw, now = this.clock.now(), pr = progress(d, now);
    var order = (d && d.draw_order) || [];
    var text = '';
    if (!d) text = 'En attente du prochain tirage';
    else if (pr.phase === 'open') text = 'Tirage dans ' + countdown(parseUtc(d.draw_time) - now);
    else if (pr.phase === 'closed') text = 'Paris fermés — tirage imminent';
    else if (pr.phase === 'intro') text = 'Le tirage commence…';
    else if (pr.phase === 'drawing') text = 'Tirage en cours';
    else if (pr.phase === 'cancelled') text = 'Tirage annulé — mises remboursées';
    else text = 'Tirage terminé';
    this.$.phase.textContent = text;
    this.root.classList.toggle('kn-spinning', pr.phase === 'intro' || pr.phase === 'drawing' || pr.phase === 'closed');
    if (this.opts.onProgress) this.opts.onProgress(pr);
    if (pr.revealed === this._shown) return;
    var fresh = this._shown >= 0 && pr.revealed - this._shown === 1;
    this._shown = pr.revealed;

    var pickSet = {};
    this.picks.forEach(function (n) { pickSet[n] = true; });
    var last = pr.revealed ? order[pr.revealed - 1] : null;
    this.$.current.innerHTML = last
      ? ballHtml(last, 'kn-big' + (fresh && !this.reduced ? ' kn-pop' : '') + (pickSet[last] ? ' kn-hit' : ''))
      : '<span class="kn-ball kn-big kn-empty">?</span>';
    this.$.count.textContent = pr.revealed + ' / ' + DRAWN;

    var drawn = {};
    this.$.slots.forEach(function (el, i) {
      var b = i < pr.revealed ? order[i] : null;
      if (b) drawn[b] = true;
      el.innerHTML = b ? ballHtml(b, pickSet[b] ? 'kn-hit' : '') : '<span class="kn-slot-no">' + (i + 1) + '</span>';
      el.classList.toggle('next', i === pr.revealed && (pr.phase === 'drawing' || pr.phase === 'intro'));
    });
    this.$.cells.forEach(function (el) {
      var n = Number(el.dataset.n);
      el.classList.toggle('drawn', !!drawn[n]);
      el.classList.toggle('pick', !!pickSet[n]);
      el.classList.toggle('hit', !!(drawn[n] && pickSet[n]));
    });
    this.$.hits.style.display = this.picks.length ? '' : 'none';
    this.$.hitsVal.textContent = hitsOf(this.picks, order, pr.revealed) + ' / ' + this.picks.length;
  };

  global.KenoLive = {
    TOTAL: TOTAL, DRAWN: DRAWN, colorOf: colorOf, ServerClock: ServerClock, parseUtc: parseUtc,
    countdown: countdown, money: money, esc: esc, ballHtml: ballHtml, progress: progress, hitsOf: hitsOf,
    connectLive: connectLive, Board: Board,
    statusLabel: function (s) { return STATUS[s] || s; }
  };
})(window);
