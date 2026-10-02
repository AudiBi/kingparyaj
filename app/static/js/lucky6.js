/* app/static/js/lucky6.js
 * Lucky6 — affichage commun (panel agent, écran de salle, écran du guichet).
 *
 * Le navigateur n'invente rien : les 35 boules, leur ordre, les cotes et les
 * gains viennent du serveur. L'animation ne fait que dévoiler, au rythme de
 * l'horloge du serveur, les boules déjà tirées : un écran rechargé ou
 * reconnecté reprend exactement au même point que les autres.
 */
(function (global) {
  'use strict';

  var TOTAL = 48, PICKS = 6, DRAWN = 35, FIRST_PAID = 6;
  var COLORS = {
    red: '#e53935', green: '#2e9d4a', blue: '#1e6fd9', purple: '#8e24aa',
    brown: '#7b4a2d', yellow: '#f2c300', orange: '#f57c00', black: '#24272b'
  };
  var COLOR_ORDER = ['red', 'green', 'blue', 'purple', 'brown', 'yellow', 'orange', 'black'];
  var STATUS_LABELS = {
    SCHEDULED: 'Programmée', BETTING_OPEN: 'Paris ouverts', BETTING_CLOSED: 'Paris fermés',
    RUNNING: 'Tirage en cours', FINISHED: 'Tirage terminé', SETTLED: 'Réglée', CANCELLED: 'Annulée'
  };

  function colorName(n) { return COLOR_ORDER[(n - 1) % COLOR_ORDER.length]; }
  function colorOf(n) { return COLORS[colorName(n)]; }
  function parseUtc(iso) { return iso ? Date.parse(iso) : null; }

  function ServerClock() { this.offset = 0; }
  ServerClock.prototype.sync = function (iso) { var s = parseUtc(iso); if (s) this.offset = s - Date.now(); };
  ServerClock.prototype.now = function () { return Date.now() + this.offset; };

  function formatCountdown(ms) {
    if (ms == null || ms <= 0) return '00:00';
    var s = Math.ceil(ms / 1000), m = Math.floor(s / 60);
    s = s % 60;
    return (m < 10 ? '0' : '') + m + ':' + (s < 10 ? '0' : '') + s;
  }

  function money(x) {
    if (x == null || isNaN(x)) return '—';
    return Number(x).toLocaleString('fr-FR', { minimumFractionDigits: 0, maximumFractionDigits: 2 }) + ' HTG';
  }

  function mult(x) {
    if (x == null) return '—';
    return 'x' + Number(x).toLocaleString('fr-FR', { maximumFractionDigits: 2 });
  }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  /* Boule (HTML) : couleur du numéro, reflet en CSS */
  function ballHtml(n, extra) {
    return '<span class="l6-ball l6-' + colorName(n) + (extra ? ' ' + extra : '') + '">' + n + '</span>';
  }

  /* Où en est le tirage, d'après l'heure du serveur.
   * -> { phase: 'waiting'|'intro'|'drawing'|'done'|'cancelled', revealed: 0..35 } */
  function progress(round, nowMs) {
    if (!round) return { phase: 'waiting', revealed: 0 };
    if (round.status === 'CANCELLED') return { phase: 'cancelled', revealed: 0 };
    var balls = round.balls || [];
    if (round.status === 'FINISHED' || round.status === 'SETTLED') return { phase: 'done', revealed: balls.length };
    if (round.status !== 'RUNNING' || !balls.length) return { phase: 'waiting', revealed: 0 };
    var t = nowMs - parseUtc(round.started_at);
    var intro = round.intro_ms || 0, step = round.ball_interval_ms || 2000;
    if (t < intro) return { phase: 'intro', revealed: 0, untilFirst: intro - t };
    var revealed = Math.min(balls.length, Math.floor((t - intro) / step) + 1);
    var sinceLast = (t - intro) - (revealed - 1) * step;
    return { phase: revealed >= balls.length && sinceLast >= step ? 'done' : 'drawing', revealed: revealed, sinceLast: sinceLast };
  }

  /* Analyse d'une sélection face aux boules dévoilées */
  function evaluate(picks, balls, revealed, paytable) {
    var set = {}, found = 0, sixth = null;
    (picks || []).forEach(function (n) { set[n] = true; });
    for (var i = 0; i < revealed; i++) {
      if (set[balls[i]]) {
        found++;
        if (found === PICKS) { sixth = i + 1; break; }
      }
    }
    var m = 0;
    if (sixth && paytable) {
      var row = paytable[sixth - FIRST_PAID];
      m = row ? (row.multiplier != null ? row.multiplier : row) : 0;
    }
    return { found: found, sixth: sixth, multiplier: m };
  }

  /* WebSocket : messages "lucky6" (canal commun), reconnexion progressive */
  function connectLive(onRound, onStatus, channel) {
    var ws = null, attempts = 0, closed = false;
    function open() {
      var proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
      try { ws = new WebSocket(proto + location.host + '/ws/draws/' + (channel || 'all')); } catch (e) { return retry(); }
      ws.onopen = function () { attempts = 0; onStatus && onStatus(true); };
      ws.onmessage = function (ev) {
        try {
          var msg = JSON.parse(ev.data);
          if (msg && msg.data && (msg.type === 'lucky6' || msg.type === 'l6_screen')) onRound(msg.data, msg.event, msg.type);
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
   * Tableau du tirage : grande boule, 35 cases avec le multiplicateur de
   * chaque position, grille 1-48, compteur de numéros trouvés.
   * ------------------------------------------------------------------ */
  function Board(root, options) {
    this.root = root;
    this.opts = options || {};
    this.clock = this.opts.clock || new ServerClock();
    this.round = null;
    this.picks = [];
    this._shown = -1;
    this._key = '';
    this.reduced = global.matchMedia && global.matchMedia('(prefers-reduced-motion: reduce)').matches;
    this._build();
  }

  Board.prototype._build = function () {
    var slots = '', grid = '';
    for (var p = 1; p <= DRAWN; p++) {
      slots += '<div class="l6-slot' + (p < FIRST_PAID ? ' l6-slot-free' : '') + '" data-pos="' + p + '">' +
        '<span class="l6-slot-ball"></span><span class="l6-slot-mult">' + (p < FIRST_PAID ? p : '') + '</span></div>';
    }
    for (var n = 1; n <= TOTAL; n++) grid += '<span class="l6-cell" data-n="' + n + '" style="--c:' + colorOf(n) + '">' + n + '</span>';
    this.root.innerHTML =
      '<div class="l6-board">' +
      '  <div class="l6-stage">' +
      '    <div class="l6-drum" aria-hidden="true"><div class="l6-drum-glow"></div><div class="l6-drum-balls"></div></div>' +
      '    <div class="l6-current"><div class="l6-current-ball" aria-live="polite"></div>' +
      '      <div class="l6-current-info"><div class="l6-count">0 / 35</div><div class="l6-phase"></div></div></div>' +
      '    <div class="l6-side">' +
      '      <div class="l6-first"><span>1re boule</span><b class="l6-first-val">—</b></div>' +
      '      <div class="l6-match"><span>Trouvés</span><b class="l6-match-val">0 / 6</b></div>' +
      '    </div>' +
      '  </div>' +
      '  <div class="l6-slots">' + slots + '</div>' +
      '  <div class="l6-grid">' + grid + '</div>' +
      '</div>';
    this.$ = {
      current: this.root.querySelector('.l6-current-ball'),
      count: this.root.querySelector('.l6-count'),
      phase: this.root.querySelector('.l6-phase'),
      first: this.root.querySelector('.l6-first-val'),
      match: this.root.querySelector('.l6-match-val'),
      matchBox: this.root.querySelector('.l6-match'),
      drumBalls: this.root.querySelector('.l6-drum-balls'),
      slots: Array.prototype.slice.call(this.root.querySelectorAll('.l6-slot')),
      cells: Array.prototype.slice.call(this.root.querySelectorAll('.l6-cell'))
    };
    var drum = '';
    for (var i = 0; i < 18; i++) {
      var k = 1 + ((i * 7) % TOTAL);
      var a = i * 20 * Math.PI / 180, rad = 26 + (i % 3) * 12;
      drum += '<span class="l6-drum-ball" style="--c:' + colorOf(k) + ';left:' + (61 + rad * Math.cos(a)).toFixed(1) +
        'px;top:' + (61 + rad * Math.sin(a)).toFixed(1) + 'px"></span>';
    }
    this.$.drumBalls.innerHTML = drum;
  };

  Board.prototype.setRound = function (round) {
    var key = round ? round.race_id + ':' + round.status + ':' + (round.balls ? round.balls.length : 0) : '';
    if (round && this.round && round.race_id !== this.round.race_id) this._shown = -1;
    this.round = round;
    if (key !== this._key) {
      this._key = key;
      this._paintMultipliers();
      this._shown = -1;
    }
    this.render();
  };

  Board.prototype.setPicks = function (picks) {
    this.picks = (picks || []).slice();
    this._shown = -1;
    this.render();
  };

  Board.prototype._paintMultipliers = function () {
    var table = (this.round && this.round.paytable) || [];
    this.$.slots.forEach(function (el, i) {
      var pos = i + 1;
      if (pos < FIRST_PAID) return;
      var row = table[pos - FIRST_PAID];
      el.querySelector('.l6-slot-mult').textContent = row && row.multiplier ? mult(row.multiplier) : '';
    });
  };

  Board.prototype.render = function () {
    var r = this.round, now = this.clock.now();
    var pr = progress(r, now);
    var balls = (r && r.balls) || [];
    var revealed = pr.revealed;
    var picks = this.picks, pickSet = {};
    picks.forEach(function (n) { pickSet[n] = true; });

    // Phase
    var phaseText = '';
    if (!r) phaseText = 'En attente de la prochaine manche';
    else if (pr.phase === 'cancelled') phaseText = 'Manche annulée — mises remboursées';
    else if (pr.phase === 'waiting') phaseText = 'Tirage dans ' + formatCountdown(parseUtc(r.scheduled_at) - now);
    else if (pr.phase === 'intro') phaseText = 'Le tirage commence…';
    else if (pr.phase === 'drawing') phaseText = 'Tirage en cours';
    else phaseText = 'Tirage terminé';
    this.$.phase.textContent = phaseText;
    this.root.classList.toggle('l6-spinning', pr.phase === 'intro' || pr.phase === 'drawing');

    if (revealed === this._shown) return;
    var fresh = revealed > this._shown && this._shown >= 0 && revealed - this._shown <= 2;
    this._shown = revealed;

    // Grande boule
    var last = revealed > 0 ? balls[revealed - 1] : null;
    if (last) {
      this.$.current.innerHTML = ballHtml(last, 'l6-big' + (fresh && !this.reduced ? ' l6-pop' : '') + (pickSet[last] ? ' l6-hit' : ''));
    } else {
      this.$.current.innerHTML = '<span class="l6-ball l6-big l6-empty">?</span>';
    }
    this.$.count.textContent = revealed + ' / ' + DRAWN;

    // 35 cases
    var ev = evaluate(picks, balls, revealed, r && r.paytable);
    this.$.slots.forEach(function (el, i) {
      var b = i < revealed ? balls[i] : null;
      var holder = el.querySelector('.l6-slot-ball');
      holder.innerHTML = b ? ballHtml(b, pickSet[b] ? 'l6-hit' : '') : '';
      el.classList.toggle('filled', !!b);
      el.classList.toggle('next', i === revealed && pr.phase !== 'done' && pr.phase !== 'waiting');
      el.classList.toggle('sixth', ev.sixth === i + 1);
    });

    // Grille 1-48
    var drawn = {};
    for (var j = 0; j < revealed; j++) drawn[balls[j]] = true;
    this.$.cells.forEach(function (el) {
      var n = Number(el.dataset.n);
      el.classList.toggle('drawn', !!drawn[n]);
      el.classList.toggle('pick', !!pickSet[n]);
      el.classList.toggle('hit', !!(drawn[n] && pickSet[n]));
    });

    // 1re boule, trouvés
    if (revealed > 0) {
      var f = balls[0];
      this.$.first.innerHTML = ballHtml(f, 'l6-mini') + ' ' + (f % 2 ? 'IMPAIR' : 'PAIR');
    } else {
      this.$.first.textContent = '—';
    }
    this.$.matchBox.style.display = picks.length ? '' : 'none';
    this.$.match.textContent = ev.found + ' / ' + PICKS + (ev.sixth ? '  ·  ' + mult(ev.multiplier) : '');
    this.$.matchBox.classList.toggle('win', !!ev.sixth);
    if (this.opts.onProgress) this.opts.onProgress(pr, ev);
  };

  global.Lucky6 = {
    TOTAL: TOTAL, PICKS: PICKS, DRAWN: DRAWN, FIRST_PAID: FIRST_PAID,
    COLORS: COLORS, colorOf: colorOf, colorName: colorName,
    ServerClock: ServerClock, parseUtc: parseUtc, formatCountdown: formatCountdown,
    statusLabel: function (s) { return STATUS_LABELS[s] || s; },
    money: money, mult: mult, esc: esc, ballHtml: ballHtml,
    progress: progress, evaluate: evaluate, connectLive: connectLive, Board: Board
  };
})(window);
