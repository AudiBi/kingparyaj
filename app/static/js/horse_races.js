/* app/static/js/horse_races.js
 * Horse Races — rendu de la course (canvas 2D) et utilitaires partagés
 * par la page agent et l'écran public.
 *
 * IMPORTANT : ce fichier ne décide de RIEN. Le classement et le scénario
 * (points de passage de chaque cheval) viennent du serveur ; on se contente
 * de les dessiner, synchronisés sur l'horloge du serveur.
 */
(function (global) {
  'use strict';

  // Couleurs de maillot par couloir (lisibles en clair et en sombre)
  var JERSEYS = [
    { shirt: '#dc2626', text: '#ffffff', trim: '#7f1d1d' },
    { shirt: '#2563eb', text: '#ffffff', trim: '#1e3a8a' },
    { shirt: '#f8fafc', text: '#0f172a', trim: '#94a3b8' },
    { shirt: '#facc15', text: '#0f172a', trim: '#a16207' },
    { shirt: '#16a34a', text: '#ffffff', trim: '#14532d' },
    { shirt: '#111827', text: '#fbbf24', trim: '#fbbf24' }
  ];
  var HORSES = ['#7c4a24', '#3f2a1d', '#a0662f', '#5b3a29', '#8b5a2b', '#2b1d14'];

  function clamp(v, a, b) { return Math.max(a, Math.min(b, v)); }

  function parseUtc(iso) { return iso ? Date.parse(iso) : null; }

  /* ------------------------------------------------------------------
   * Horloge serveur : décalage entre l'heure du serveur et celle du poste
   * ------------------------------------------------------------------ */
  function ServerClock() { this.offset = 0; }
  ServerClock.prototype.sync = function (serverIso) {
    var server = parseUtc(serverIso);
    if (server) this.offset = server - Date.now();
  };
  ServerClock.prototype.now = function () { return Date.now() + this.offset; };

  function formatCountdown(ms) {
    if (ms == null || ms <= 0) return '00:00';
    var s = Math.ceil(ms / 1000);
    var m = Math.floor(s / 60);
    s = s % 60;
    return (m < 10 ? '0' : '') + m + ':' + (s < 10 ? '0' : '') + s;
  }

  var STATUS_LABELS = {
    SCHEDULED: 'Programmée',
    BETTING_OPEN: 'Paris ouverts',
    BETTING_CLOSED: 'Paris fermés',
    RUNNING: 'Course en cours',
    FINISHED: 'Arrivée',
    SETTLED: 'Terminée',
    CANCELLED: 'Annulée'
  };

  /* ------------------------------------------------------------------
   * Renderer
   * ------------------------------------------------------------------ */
  function Renderer(canvas, options) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.options = options || {};
    this.clock = this.options.clock || new ServerClock();
    this.race = null;
    this.images = {};
    this.reducedMotion = global.matchMedia && global.matchMedia('(prefers-reduced-motion: reduce)').matches;
    this.lowPower = (navigator.hardwareConcurrency || 4) <= 2;
    this._frame = null;
    this._lastDraw = 0;
    this._resize = this.resize.bind(this);
    this._visibility = this._onVisibility.bind(this);
    var logoUrl = this.options.logoUrl === undefined ? '/static/img/logo-256.png' : this.options.logoUrl;
    if (logoUrl) {
      var logo = new Image();
      logo.onload = function () { logo._ready = true; };
      logo.src = logoUrl;
      this.logo = logo;
    }
    global.addEventListener('resize', this._resize);
    document.addEventListener('visibilitychange', this._visibility);
    this.resize();
    this.start();
  }

  Renderer.prototype.setRace = function (race) {
    this.race = race;
    if (race && race.server_time) this.clock.sync(race.server_time);
    this._loadAssets(race);
  };

  /** Chevaux du joueur à repérer pendant la course (écran du guichet). */
  Renderer.prototype.setHighlight = function (numbers) {
    this.highlight = (numbers || []).map(Number);
  };

  Renderer.prototype._marker = function (x, y, size) {
    var ctx = this.ctx, r = Math.max(8, size * 0.13);
    ctx.save();
    ctx.fillStyle = '#fbbf24';
    ctx.strokeStyle = '#0f172a';
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(x - r, y - r * 1.6); ctx.lineTo(x + r, y - r * 1.6); ctx.lineTo(x, y);
    ctx.closePath(); ctx.fill(); ctx.stroke();
    ctx.font = '900 ' + Math.round(r * 1.1) + 'px Inter, system-ui, sans-serif';
    ctx.textAlign = 'center'; ctx.textBaseline = 'bottom';
    ctx.lineWidth = 3; ctx.strokeText('VOUS', x, y - r * 1.75);
    ctx.fillText('VOUS', x, y - r * 1.75);
    ctx.restore();
  };

  Renderer.prototype._loadAssets = function (race) {
    var self = this;
    if (!race) return;
    race.participants.forEach(function (p) {
      var assets = p.assets || {};
      ['rider_back', 'horse_side'].forEach(function (key) {
        var url = assets[key];
        if (url && !self.images[url]) {
          var img = new Image();
          img.onload = function () { img._ready = true; };
          img.src = url;
          self.images[url] = img;
        }
      });
    });
  };

  Renderer.prototype._image = function (participant, key) {
    var url = (participant.assets || {})[key];
    var img = url && this.images[url];
    return img && img._ready ? img : null;
  };

  Renderer.prototype.resize = function () {
    var dpr = Math.min(global.devicePixelRatio || 1, this.lowPower ? 1 : 2);
    var width = this.canvas.clientWidth || this.canvas.parentNode.clientWidth || 640;
    var height = Math.round(clamp(width * 0.56, 260, this.options.maxHeight || 620));
    this.canvas.style.height = height + 'px';
    this.canvas.width = Math.round(width * dpr);
    this.canvas.height = Math.round(height * dpr);
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.w = width;
    this.h = height;
  };

  Renderer.prototype.start = function () {
    var self = this;
    var minGap = this.lowPower || this.reducedMotion ? 1000 / 30 : 0; // 30 i/s sur les appareils modestes
    function loop(ts) {
      self._frame = global.requestAnimationFrame(loop);
      if (ts - self._lastDraw < minGap) return;
      self._lastDraw = ts;
      self.draw(ts);
    }
    this._frame = global.requestAnimationFrame(loop);
  };

  Renderer.prototype.stop = function () {
    if (this._frame) global.cancelAnimationFrame(this._frame);
    this._frame = null;
  };

  Renderer.prototype._onVisibility = function () {
    if (document.hidden) this.stop(); else if (!this._frame) this.start();
  };

  Renderer.prototype.destroy = function () {
    this.stop();
    global.removeEventListener('resize', this._resize);
    document.removeEventListener('visibilitychange', this._visibility);
  };

  /** Phase et avancement (0..1) de l'animation, selon l'horloge du serveur. */
  Renderer.prototype.phase = function () {
    var race = this.race;
    if (!race) return { name: 'empty' };
    if (race.status === 'CANCELLED') return { name: 'cancelled' };
    if (race.status === 'RUNNING' || race.status === 'FINISHED' || race.status === 'SETTLED') {
      var script = race.race_script;
      var started = parseUtc(race.started_at);
      if (!script || !started) return { name: 'results' };
      var t = (this.clock.now() - started) / script.duration_ms;
      if (t < 0) return { name: 'gate' };
      if (t >= 1.12) return { name: 'results', t: 1 };
      return { name: 'race', t: Math.min(t, 1), raw: t };
    }
    return { name: 'gate' };
  };

  Renderer.prototype.progressAt = function (t) {
    var frames = this.race.race_script.frames;
    var out = {};
    if (t <= frames[0].t) return frames[0].progress;
    if (t >= frames[frames.length - 1].t) return frames[frames.length - 1].progress;
    for (var i = 1; i < frames.length; i++) {
      if (t <= frames[i].t) {
        var a = frames[i - 1], b = frames[i];
        var k = (t - a.t) / (b.t - a.t);
        k = k * k * (3 - 2 * k); // lissage
        Object.keys(b.progress).forEach(function (n) {
          out[n] = a.progress[n] + (b.progress[n] - a.progress[n]) * k;
        });
        return out;
      }
    }
    return out;
  };

  Renderer.prototype.draw = function (ts) {
    var ctx = this.ctx;
    ctx.clearRect(0, 0, this.w, this.h);
    var phase = this.phase();
    if (phase.name === 'empty') return this._drawMessage('En attente de la prochaine course…');
    if (phase.name === 'cancelled') return this._drawMessage('Course annulée — mises remboursées');
    if (phase.name === 'gate') return this._drawGate(ts);
    if (phase.name === 'race') return this._drawRace(phase.t, phase.raw, ts);
    return this._drawResults(ts);
  };

  Renderer.prototype._drawMessage = function (text) {
    var ctx = this.ctx;
    this._drawSky();
    ctx.fillStyle = '#ffffff';
    ctx.font = '600 ' + Math.round(this.w / 32) + 'px Inter, system-ui, sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(text, this.w / 2, this.h / 2);
  };

  /** Logo King Paryaj (s'il est chargé), coin de l'image. */
  Renderer.prototype._drawLogo = function (x, y, size, alpha) {
    if (!this.logo || !this.logo._ready) return;
    var ctx = this.ctx;
    ctx.save();
    ctx.globalAlpha = alpha == null ? 1 : alpha;
    ctx.drawImage(this.logo, x, y, size, size);
    ctx.restore();
  };

  Renderer.prototype._drawSky = function () {
    var ctx = this.ctx;
    var g = ctx.createLinearGradient(0, 0, 0, this.h);
    g.addColorStop(0, '#0f172a');
    g.addColorStop(1, '#14532d');
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, this.w, this.h);
  };

  /* ---------------- Départ : cavaliers vus de dos ---------------- */
  Renderer.prototype._drawGate = function (ts) {
    var ctx = this.ctx, w = this.w, h = this.h;
    var parts = this.race.participants;
    // piste en perspective
    this._drawSky();
    ctx.fillStyle = '#166534';
    ctx.beginPath();
    ctx.moveTo(w * 0.42, h * 0.28); ctx.lineTo(w * 0.58, h * 0.28);
    ctx.lineTo(w, h); ctx.lineTo(0, h); ctx.closePath(); ctx.fill();
    ctx.strokeStyle = 'rgba(255,255,255,0.35)';
    ctx.lineWidth = 2;
    for (var i = 0; i <= parts.length; i++) {
      ctx.beginPath();
      ctx.moveTo(w * 0.42 + (w * 0.16) * i / parts.length, h * 0.28);
      ctx.lineTo(w * i / parts.length, h);
      ctx.stroke();
    }
    // ligne d'arrivée au loin
    ctx.fillStyle = '#e2e8f0';
    ctx.fillRect(w * 0.42, h * 0.27, w * 0.16, 4);

    this._drawLogo(w * 0.015, h * 0.02, Math.min(h * 0.24, w * 0.16));
    var slot = w / parts.length;
    var self = this;
    parts.forEach(function (p, idx) {
      var idle = self.reducedMotion ? 0 : ts / 420 + idx * 1.3;
      self._drawRiderBack(p, idx, slot * idx + slot / 2, h * 0.97, Math.min(slot * 1.25, h * 0.66), idle);
    });
  };

  /* ---------- Robes de chevaux réalistes (bai, alezan, gris, noir…) ---------- */
  var COATS = [
    { name: 'bai',        top: '#8a4b22', mid: '#6b3416', dark: '#3a1a0a', mane: '#1a0f0a', legs: '#1f140e', socks: [] },
    { name: 'alezan',     top: '#b8652e', mid: '#9a4f1f', dark: '#5e2c0f', mane: '#7a3a16', legs: '#8a4319', socks: [1], blaze: true },
    { name: 'gris',       top: '#d6d3d1', mid: '#a8a29e', dark: '#57534e', mane: '#e7e5e4', legs: '#78716c', socks: [] },
    { name: 'bai brun',   top: '#5a3420', mid: '#3d2214', dark: '#1c0f08', mane: '#0c0806', legs: '#120b07', socks: [3] },
    { name: 'noir',       top: '#3a3532', mid: '#231f1d', dark: '#0d0b0a', mane: '#050404', legs: '#0b0a09', socks: [0, 2], blaze: true },
    { name: 'palomino',   top: '#d9a85c', mid: '#c08a3e', dark: '#7a5320', mane: '#f1e4c8', legs: '#a8742f', socks: [] }
  ];

  function coatFor(lane) { return COATS[lane % COATS.length]; }

  function lerp(a, b, t) { return a + (b - a) * t; }

  /** Trace un contour lissé passant par les milieux des points (courbes quadratiques). */
  function smoothShape(ctx, pts, ox, oy, u) {
    var n = pts.length;
    function P(i) { var p = pts[(i + n) % n]; return [ox + p[0] * u, oy + p[1] * u]; }
    var a = P(0), b = P(1);
    ctx.beginPath();
    ctx.moveTo((a[0] + b[0]) / 2, (a[1] + b[1]) / 2);
    for (var i = 1; i <= n; i++) {
      var c = P(i), d = P(i + 1);
      ctx.quadraticCurveTo(c[0], c[1], (c[0] + d[0]) / 2, (c[1] + d[1]) / 2);
    }
    ctx.closePath();
  }

  /** Segment de membre effilé (largeurs w1 -> w2), extrémités arrondies. */
  function limb(ctx, x1, y1, x2, y2, w1, w2) {
    var dx = x2 - x1, dy = y2 - y1, len = Math.sqrt(dx * dx + dy * dy) || 1;
    var nx = -dy / len, ny = dx / len;
    ctx.beginPath();
    ctx.moveTo(x1 + nx * w1 / 2, y1 + ny * w1 / 2);
    ctx.lineTo(x2 + nx * w2 / 2, y2 + ny * w2 / 2);
    ctx.lineTo(x2 - nx * w2 / 2, y2 - ny * w2 / 2);
    ctx.lineTo(x1 - nx * w1 / 2, y1 - ny * w1 / 2);
    ctx.closePath();
    ctx.fill();
    ctx.beginPath(); ctx.arc(x1, y1, w1 / 2, 0, Math.PI * 2); ctx.fill();
    ctx.beginPath(); ctx.arc(x2, y2, w2 / 2, 0, Math.PI * 2); ctx.fill();
  }

  function shade(ctx, x0, y0, x1, y1, stops) {
    var g = ctx.createLinearGradient(x0, y0, x1, y1);
    stops.forEach(function (s) { g.addColorStop(s[0], s[1]); });
    return g;
  }

  /* ---------------- Cavalier et cheval vus de dos ---------------- */
  Renderer.prototype._drawRiderBack = function (p, lane, cx, baseY, size, idle) {
    var ctx = this.ctx;
    var img = this._image(p, 'rider_back');
    if (img) { ctx.drawImage(img, cx - size / 2, baseY - size, size, size); return; }
    var j = JERSEYS[lane % JERSEYS.length];
    var c = coatFor(lane);
    var u = size / 1.42;              // hauteur totale ≈ 1.42 u (sabots -> casque)
    idle = idle || 0;
    var shift = Math.sin(idle) * u * 0.012;   // le cheval piétine, le poids passe d'une jambe à l'autre
    var X = function (v) { return cx + v * u; };
    var Y = function (v) { return baseY + v * u; };

    ctx.save();
    // ombre au sol
    ctx.fillStyle = 'rgba(0,0,0,0.28)';
    ctx.beginPath(); ctx.ellipse(cx, baseY, u * 0.34, u * 0.05, 0, 0, Math.PI * 2); ctx.fill();

    // ventre / côtes (plus large que l'arrière-main, visible de dos)
    ctx.fillStyle = shade(ctx, X(-0.3), 0, X(0.3), 0, [[0, c.dark], [0.5, c.mid], [1, c.dark]]);
    ctx.beginPath(); ctx.ellipse(X(0), Y(-0.72), u * 0.35, u * 0.18, 0, 0, Math.PI * 2); ctx.fill();

    // postérieurs : grasset -> jarret -> canon -> boulet -> sabot
    [-1, 1].forEach(function (side) {
      var lift = side > 0 ? Math.max(0, shift) : Math.max(0, -shift);
      var hx = X(side * 0.12), top = Y(-0.58);
      var hockX = X(side * 0.125), hockY = Y(-0.32) - lift;
      var fetX = X(side * 0.12), fetY = Y(-0.09) - lift;
      ctx.fillStyle = shade(ctx, hx - u * 0.1, 0, hx + u * 0.1, 0, [[0, c.dark], [0.5, c.mid], [1, c.dark]]);
      limb(ctx, hx, top, hockX, hockY, u * 0.20, u * 0.09);          // jambe musclée
      ctx.fillStyle = (c.socks.indexOf(side > 0 ? 3 : 2) >= 0) ? '#f5f5f4' : c.legs;
      limb(ctx, hockX, hockY, fetX, fetY, u * 0.085, u * 0.065);     // canon
      ctx.beginPath(); ctx.arc(hockX, hockY + u * 0.005, u * 0.045, 0, Math.PI * 2); ctx.fill();  // pointe du jarret
      ctx.fillStyle = '#1c1917';
      ctx.beginPath(); ctx.ellipse(fetX, Y(-0.025) - lift, u * 0.045, u * 0.03, 0, 0, Math.PI * 2); ctx.fill(); // sabot
    });

    // arrière-main : deux fesses arrondies, sillon central
    var qy = Y(-0.72);
    var rump = ctx.createRadialGradient(X(-0.06), Y(-0.86), u * 0.02, X(0), qy, u * 0.34);
    rump.addColorStop(0, c.top); rump.addColorStop(0.6, c.mid); rump.addColorStop(1, c.dark);
    ctx.fillStyle = rump;
    ctx.beginPath();
    ctx.moveTo(X(0), Y(-0.92));
    ctx.bezierCurveTo(X(0.14), Y(-0.96), X(0.32), Y(-0.88), X(0.31), Y(-0.70));
    ctx.bezierCurveTo(X(0.30), Y(-0.54), X(0.13), Y(-0.48), X(0.02), Y(-0.54));
    ctx.lineTo(X(-0.02), Y(-0.55));
    ctx.bezierCurveTo(X(-0.13), Y(-0.48), X(-0.30), Y(-0.54), X(-0.31), Y(-0.70));
    ctx.bezierCurveTo(X(-0.32), Y(-0.88), X(-0.14), Y(-0.96), X(0), Y(-0.92));
    ctx.fill();
    ctx.strokeStyle = 'rgba(0,0,0,0.35)'; ctx.lineWidth = Math.max(1, u * 0.012);
    ctx.beginPath(); ctx.moveTo(X(0), Y(-0.86)); ctx.quadraticCurveTo(X(0.01), Y(-0.70), X(0), Y(-0.56)); ctx.stroke();

    // queue : masse de crins qui tombe et ondule
    var sw = Math.sin(idle * 0.7) * u * 0.035;
    ctx.fillStyle = c.mane;
    ctx.beginPath();
    ctx.moveTo(X(-0.03), Y(-0.91));
    ctx.quadraticCurveTo(X(-0.05), Y(-0.70), X(-0.075) + sw, Y(-0.40));
    ctx.quadraticCurveTo(X(0) + sw, Y(-0.35), X(0.075) + sw, Y(-0.40));
    ctx.quadraticCurveTo(X(0.05), Y(-0.70), X(0.03), Y(-0.91));
    ctx.closePath(); ctx.fill();
    ctx.strokeStyle = 'rgba(255,255,255,0.12)'; ctx.lineWidth = Math.max(1, u * 0.008);
    for (var k = -2; k <= 2; k++) {
      ctx.beginPath(); ctx.moveTo(X(k * 0.008), Y(-0.86));
      ctx.quadraticCurveTo(X(k * 0.02), Y(-0.62), X(k * 0.03) + sw, Y(-0.42)); ctx.stroke();
    }

    // tapis de selle : rabats visibles de chaque côté, avec le numéro
    [-1, 1].forEach(function (side) {
      ctx.fillStyle = j.shirt;
      ctx.beginPath();
      ctx.moveTo(X(side * 0.24), Y(-0.95)); ctx.lineTo(X(side * 0.33), Y(-0.93));
      ctx.lineTo(X(side * 0.33), Y(-0.80)); ctx.lineTo(X(side * 0.25), Y(-0.80)); ctx.closePath(); ctx.fill();
      ctx.fillStyle = j.text; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
      ctx.font = '800 ' + Math.max(6, u * 0.07) + 'px Inter, system-ui, sans-serif';
      ctx.fillText(String(p.player_number), X(side * 0.29), Y(-0.865));
    });

    // jockey accroupi, vu de dos : bottes dans les étriers, culotte blanche, casaque
    [-1, 1].forEach(function (side) {
      ctx.fillStyle = '#f8fafc';                                   // culotte
      limb(ctx, X(side * 0.10), Y(-1.04), X(side * 0.25), Y(-0.98), u * 0.10, u * 0.075);
      limb(ctx, X(side * 0.25), Y(-0.98), X(side * 0.21), Y(-0.86), u * 0.07, u * 0.055);
      ctx.fillStyle = '#111827';                                   // botte
      limb(ctx, X(side * 0.215), Y(-0.89), X(side * 0.205), Y(-0.80), u * 0.06, u * 0.05);
      ctx.fillStyle = '#9ca3af';                                   // étrier
      ctx.fillRect(X(side * 0.205) - u * 0.035, Y(-0.79), u * 0.07, u * 0.012);
    });

    // dos de la casaque (penché en avant : le dos apparaît large et court)
    var backTop = Y(-1.30), backBottom = Y(-1.02);
    ctx.fillStyle = shade(ctx, X(-0.22), 0, X(0.22), 0, [[0, j.trim], [0.12, j.shirt], [0.88, j.shirt], [1, j.trim]]);
    ctx.beginPath();
    ctx.moveTo(X(-0.21), backTop + u * 0.02);
    ctx.quadraticCurveTo(X(0), backTop - u * 0.03, X(0.21), backTop + u * 0.02);
    ctx.lineTo(X(0.15), backBottom);
    ctx.quadraticCurveTo(X(0), backBottom + u * 0.03, X(-0.15), backBottom);
    ctx.closePath(); ctx.fill();
    // bras : coudes écartés, mains vers l'encolure
    ctx.fillStyle = j.shirt;
    limb(ctx, X(-0.19), Y(-1.27), X(-0.29), Y(-1.15), u * 0.08, u * 0.07);
    limb(ctx, X(0.19), Y(-1.27), X(0.29), Y(-1.15), u * 0.08, u * 0.07);
    ctx.fillStyle = j.trim;                                         // manchettes
    ctx.beginPath(); ctx.arc(X(-0.29), Y(-1.15), u * 0.035, 0, Math.PI * 2); ctx.fill();
    ctx.beginPath(); ctx.arc(X(0.29), Y(-1.15), u * 0.035, 0, Math.PI * 2); ctx.fill();

    // nom puis numéro, comme au dos d'un maillot de football
    ctx.fillStyle = j.text; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    var name = String(p.player_name || '').toUpperCase();
    ctx.font = '800 ' + Math.max(7, u * 0.068) + 'px Inter, system-ui, sans-serif';
    ctx.fillText(name, X(0), Y(-1.235), u * 0.36);
    ctx.font = '900 ' + Math.max(9, u * 0.15) + 'px Inter, system-ui, sans-serif';
    ctx.fillText(String(p.player_number), X(0), Y(-1.11), u * 0.3);

    // nuque, casque (calotte aux couleurs), lunettes relevées
    ctx.fillStyle = '#8d5a3b';
    ctx.fillRect(X(-0.03), Y(-1.33), u * 0.06, u * 0.04);
    ctx.fillStyle = shade(ctx, X(-0.08), 0, X(0.08), 0, [[0, j.trim], [0.5, j.shirt], [1, j.trim]]);
    ctx.beginPath(); ctx.ellipse(X(0), Y(-1.375), u * 0.078, u * 0.07, 0, Math.PI, 0); ctx.fill();
    ctx.fillRect(X(-0.078), Y(-1.38), u * 0.156, u * 0.03);
    ctx.fillStyle = '#111827';
    ctx.fillRect(X(-0.078), Y(-1.36), u * 0.156, u * 0.012);       // sangle des lunettes
    ctx.restore();
  };

  /* ---------------- Course : vue de côté ---------------- */
  Renderer.prototype._layout = function () {
    var parts = this.race.participants;
    var top = this.h * 0.12, bottom = this.h * 0.96;
    var laneH = (bottom - top) / parts.length;
    var labelW = this.w < 520 ? 44 : Math.min(150, this.w * 0.18);
    return {
      top: top, laneH: laneH, labelW: labelW,
      startX: labelW + 10, finishX: this.w - Math.max(30, this.w * 0.06)
    };
  };

  Renderer.prototype._drawTrack = function (L, scroll) {
    var ctx = this.ctx, w = this.w, h = this.h;
    var parts = this.race.participants;
    ctx.fillStyle = '#0f172a'; ctx.fillRect(0, 0, w, h);
    // tribunes au loin
    ctx.fillStyle = '#1e293b'; ctx.fillRect(0, 0, w, L.top);
    ctx.fillStyle = 'rgba(148,163,184,0.25)';
    for (var x = -((scroll * 0.3) % 18); x < w; x += 18) ctx.fillRect(x, L.top * 0.35, 9, L.top * 0.4);
    for (var i = 0; i < parts.length; i++) {
      ctx.fillStyle = i % 2 ? '#15803d' : '#166534';
      ctx.fillRect(0, L.top + i * L.laneH, w, L.laneH);
    }
    this._drawLogo(w - L.top * 1.05 - 6, 2, L.top * 1.05 - 4, 0.95);
    // poteaux de la lice qui défilent (impression de vitesse)
    ctx.fillStyle = 'rgba(255,255,255,0.55)';
    for (var px = -(scroll % 60); px < w; px += 60) ctx.fillRect(px, L.top - 4, 3, 8);
    // départ et arrivée
    ctx.fillStyle = 'rgba(255,255,255,0.4)';
    ctx.fillRect(L.startX, L.top, 2, L.laneH * parts.length);
    var sq = Math.max(5, L.laneH / 6);
    for (var y = L.top, k = 0; y < L.top + L.laneH * parts.length; y += sq, k++) {
      ctx.fillStyle = k % 2 ? '#0f172a' : '#ffffff';
      ctx.fillRect(L.finishX, y, sq, sq);
      ctx.fillStyle = k % 2 ? '#ffffff' : '#0f172a';
      ctx.fillRect(L.finishX + sq, y, sq, sq);
    }
    // étiquettes de couloir : №10 MESSI
    ctx.textBaseline = 'middle'; ctx.textAlign = 'left';
    for (var n = 0; n < parts.length; n++) {
      var p = parts[n], j = JERSEYS[n % JERSEYS.length];
      var cy = L.top + n * L.laneH + L.laneH / 2;
      ctx.fillStyle = 'rgba(15,23,42,0.75)'; ctx.fillRect(0, cy - L.laneH * 0.38, L.labelW, L.laneH * 0.76);
      ctx.fillStyle = j.shirt; ctx.fillRect(0, cy - L.laneH * 0.38, 5, L.laneH * 0.76);
      ctx.fillStyle = '#ffffff';
      ctx.font = '700 ' + Math.round(clamp(L.laneH * 0.32, 10, 18)) + 'px Inter, system-ui, sans-serif';
      var label = this.w < 520 ? '№' + p.player_number : '№' + p.player_number + ' ' + String(p.player_name).toUpperCase();
      ctx.fillText(label, 10, cy, L.labelW - 14);
    }
  };

  /* ---------------- Cheval au galop et jockey, vue de côté ----------------
   * Squelette simple : chaque jambe a 3 segments (avant-bras/jambe, canon,
   * paturon) dont les angles suivent un cycle de galop à 4 temps
   * (postérieur, postérieur, antérieur, antérieur, puis temps de suspension).
   */
  var GALLOP = { hindFar: 0.00, hindNear: 0.08, foreFar: 0.24, foreNear: 0.32 };

  function legAngles(q, front) {
    q = q - Math.floor(q);
    var A = front ? 0.62 : 0.52, F = front ? 1.9 : 1.25, stance = 0.40;
    if (q < stance) {                       // appui : la jambe balaie vers l'arrière
      var t = q / stance;
      return { a: lerp(A, -A * 0.85, t), flex: 0.12 * Math.sin(Math.PI * t), ground: true };
    }
    var s = (q - stance) / (1 - stance);    // soutien : repliée puis lancée vers l'avant
    var e = s * s * (3 - 2 * s);
    return { a: lerp(-A * 0.85, A, e), flex: Math.sin(Math.PI * Math.min(1, s * 1.15)) * F, ground: false };
  }

  Renderer.prototype._drawHorseSide = function (p, lane, x, cy, size, phase) {
    var ctx = this.ctx;
    var img = this._image(p, 'horse_side');
    if (img) { ctx.drawImage(img, x - size * 1.2, cy - size * 0.75, size * 1.3, size); return; }
    var j = JERSEYS[lane % JERSEYS.length];
    var c = coatFor(lane);
    var u = size * 0.78;                       // le cheval mesure ~1.35 u de long
    var still = phase === null || phase === undefined;
    phase = still ? 0.15 : phase;
    var bob = still ? 0 : Math.sin(phase * Math.PI * 2) * u * 0.025;
    var ox = x - 0.74 * u;                     // x = bout du nez
    var oy = cy + 0.48 * u + bob;              // sol (sabots)
    var X = function (v) { return ox + v * u; };
    var Y = function (v) { return oy + v * u; };

    ctx.save();
    // ombre
    ctx.fillStyle = 'rgba(0,0,0,0.25)';
    ctx.beginPath(); ctx.ellipse(X(0.05), oy - bob + u * 0.01, u * 0.55, u * 0.05, 0, 0, Math.PI * 2); ctx.fill();

    function drawLeg(key, front, far) {
      var g = legAngles(phase + GALLOP[key], front);
      var root = front ? [0.27, -0.60] : [-0.40, -0.66];
      var L = front ? [0.27, 0.22, 0.085] : [0.30, 0.25, 0.085];
      var a1 = front ? g.a : g.a - 0.30;
      var a2 = front ? a1 - g.flex * 1.35 : a1 + 0.30 + g.flex * 0.85;
      var a3 = g.ground ? a2 + 0.55 : a2 - g.flex * (front ? 0.9 : 0.5) + 0.2;
      var x0 = X(root[0]), y0 = Y(root[1]);
      var x1 = x0 + Math.sin(a1) * L[0] * u, y1 = y0 + Math.cos(a1) * L[0] * u;
      var x2 = x1 + Math.sin(a2) * L[1] * u, y2 = y1 + Math.cos(a2) * L[1] * u;
      var x3 = x2 + Math.sin(a3) * L[2] * u, y3 = y2 + Math.cos(a3) * L[2] * u;
      var sock = c.socks.indexOf({ foreNear: 0, foreFar: 1, hindFar: 2, hindNear: 3 }[key]) >= 0;
      ctx.globalAlpha = 1;
      ctx.fillStyle = far ? c.dark : shade(ctx, x0 - u * 0.06, 0, x0 + u * 0.06, 0, [[0, c.mid], [1, c.dark]]);
      limb(ctx, x0, y0, x1, y1, u * (front ? 0.10 : 0.13), u * 0.06);          // avant-bras / jambe
      ctx.fillStyle = far ? c.legs : (sock ? '#f5f5f4' : c.legs);
      if (far) ctx.fillStyle = shadeDark(c.legs);
      limb(ctx, x1, y1, x2, y2, u * 0.055, u * 0.045);                         // canon
      limb(ctx, x2, y2, x3, y3, u * 0.05, u * 0.04);                           // paturon
      ctx.fillStyle = '#1c1917';                                               // sabot
      ctx.save(); ctx.translate(x3, y3); ctx.rotate(-a3);
      ctx.beginPath(); ctx.moveTo(-u * 0.03, -u * 0.005); ctx.lineTo(u * 0.035, -u * 0.005);
      ctx.lineTo(u * 0.045, u * 0.04); ctx.lineTo(-u * 0.03, u * 0.04); ctx.closePath(); ctx.fill();
      ctx.restore();
    }

    // jambes du côté éloigné (plus sombres, derrière le corps)
    drawLeg('hindFar', false, true);
    drawLeg('foreFar', true, true);

    // queue
    var wave = still ? 0 : Math.sin(phase * Math.PI * 2 + 1) * u * 0.05;
    ctx.strokeStyle = c.mane; ctx.lineCap = 'round';
    for (var k = 0; k < 5; k++) {
      ctx.lineWidth = u * (0.045 - k * 0.006);
      ctx.beginPath();
      ctx.moveTo(X(-0.52), Y(-0.86));
      ctx.bezierCurveTo(X(-0.70), Y(-0.86) + wave, X(-0.80), Y(-0.74 + k * 0.02) - wave, X(-0.92 + k * 0.01), Y(-0.62 + k * 0.03) + wave);
      ctx.stroke();
    }

    // corps : dos, encolure tendue, tête, poitrail, ventre, arrière-main
    var nod = still ? 0 : Math.sin(phase * Math.PI * 2 - 0.6) * 0.03;
    var body = [
      [-0.44, -0.93], [-0.24, -0.90], [-0.05, -0.87], [0.14, -0.92], [0.28, -1.00],
      [0.40, -1.08 + nod], [0.50, -1.15 + nod], [0.56, -1.15 + nod], [0.63, -1.07 + nod],
      [0.71, -0.97 + nod], [0.765, -0.90 + nod], [0.77, -0.85 + nod], [0.72, -0.825 + nod],
      [0.63, -0.84 + nod], [0.55, -0.855 + nod], [0.50, -0.89 + nod], [0.47, -0.93 + nod],
      [0.42, -0.86], [0.37, -0.73], [0.29, -0.60], [0.12, -0.53], [-0.12, -0.54], [-0.30, -0.60],
      [-0.46, -0.66], [-0.55, -0.78]
    ];
    var g = ctx.createLinearGradient(0, Y(-1.1), 0, Y(-0.5));
    g.addColorStop(0, c.top); g.addColorStop(0.55, c.mid); g.addColorStop(1, c.dark);
    ctx.fillStyle = g;
    smoothShape(ctx, body, ox, oy, u);
    ctx.fill();
    // modelé : épaule et arrière-main
    var hl = ctx.createRadialGradient(X(-0.36), Y(-0.80), u * 0.02, X(-0.36), Y(-0.80), u * 0.2);
    hl.addColorStop(0, 'rgba(255,255,255,0.18)'); hl.addColorStop(1, 'rgba(255,255,255,0)');
    ctx.fillStyle = hl; ctx.beginPath(); ctx.arc(X(-0.36), Y(-0.80), u * 0.2, 0, Math.PI * 2); ctx.fill();
    hl = ctx.createRadialGradient(X(0.24), Y(-0.78), u * 0.02, X(0.24), Y(-0.78), u * 0.16);
    hl.addColorStop(0, 'rgba(255,255,255,0.14)'); hl.addColorStop(1, 'rgba(255,255,255,0)');
    ctx.fillStyle = hl; ctx.beginPath(); ctx.arc(X(0.24), Y(-0.78), u * 0.16, 0, Math.PI * 2); ctx.fill();

    // crinière, oreille, œil, naseau, liste blanche
    ctx.strokeStyle = c.mane; ctx.lineWidth = u * 0.03;
    ctx.beginPath(); ctx.moveTo(X(0.17), Y(-0.94)); ctx.quadraticCurveTo(X(0.36), Y(-1.08 + nod), X(0.52), Y(-1.12 + nod)); ctx.stroke();
    for (var m = 0; m < 5; m++) {
      var mx = lerp(0.20, 0.48, m / 4), my = lerp(-0.96, -1.10, m / 4) + nod * (m / 4);
      ctx.lineWidth = u * 0.018;
      ctx.beginPath(); ctx.moveTo(X(mx), Y(my)); ctx.lineTo(X(mx - 0.05), Y(my + 0.03 + (still ? 0 : Math.sin(phase * 6.3 + m) * 0.015))); ctx.stroke();
    }
    ctx.fillStyle = c.mid;
    ctx.beginPath(); ctx.moveTo(X(0.50), Y(-1.13 + nod)); ctx.lineTo(X(0.52), Y(-1.23 + nod)); ctx.lineTo(X(0.56), Y(-1.13 + nod)); ctx.fill();
    if (c.blaze) {
      ctx.strokeStyle = 'rgba(255,255,255,0.85)'; ctx.lineWidth = u * 0.02;
      ctx.beginPath(); ctx.moveTo(X(0.61), Y(-1.07 + nod)); ctx.lineTo(X(0.745), Y(-0.90 + nod)); ctx.stroke();
    }
    ctx.fillStyle = '#0a0a0a';
    ctx.beginPath(); ctx.arc(X(0.575), Y(-1.075 + nod), u * 0.016, 0, Math.PI * 2); ctx.fill();
    ctx.beginPath(); ctx.arc(X(0.745), Y(-0.885 + nod), u * 0.011, 0, Math.PI * 2); ctx.fill();

    // harnachement : bride, rênes, tapis de selle numéroté, selle, sangle
    ctx.strokeStyle = '#111827'; ctx.lineWidth = Math.max(1, u * 0.012);
    ctx.beginPath(); ctx.moveTo(X(0.545), Y(-1.13 + nod)); ctx.lineTo(X(0.60), Y(-0.89 + nod)); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(X(0.60), Y(-1.04 + nod)); ctx.lineTo(X(0.72), Y(-0.96 + nod)); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(X(0.60), Y(-0.89 + nod)); ctx.lineTo(X(0.71), Y(-0.86 + nod)); ctx.stroke();
    ctx.fillStyle = j.shirt;
    ctx.beginPath();
    ctx.moveTo(X(-0.17), Y(-0.89)); ctx.lineTo(X(0.12), Y(-0.90)); ctx.lineTo(X(0.10), Y(-0.70)); ctx.lineTo(X(-0.15), Y(-0.70));
    ctx.closePath(); ctx.fill();
    ctx.strokeStyle = j.trim; ctx.lineWidth = Math.max(1, u * 0.01); ctx.stroke();
    ctx.fillStyle = j.text; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    ctx.font = '900 ' + Math.max(7, u * 0.13) + 'px Inter, system-ui, sans-serif';
    ctx.fillText(String(p.player_number), X(-0.025), Y(-0.795));
    ctx.fillStyle = '#1f2937';
    ctx.beginPath(); ctx.ellipse(X(-0.02), Y(-0.905), u * 0.13, u * 0.03, -0.05, 0, Math.PI * 2); ctx.fill();
    ctx.fillStyle = 'rgba(31,41,55,0.85)';
    ctx.fillRect(X(0.105), Y(-0.70), u * 0.018, u * 0.16);          // sangle sous le ventre

    // jambes du côté visible (devant le corps)
    drawLeg('hindNear', false, false);
    drawLeg('foreNear', true, false);

    // ---- jockey en position de course (accroupi, buste presque horizontal) ----
    var ry = still ? 0 : -bob * 0.7;          // le jockey amortit : il reste stable
    var R = function (v) { return Y(v) + ry; };
    // rênes
    ctx.strokeStyle = '#3f2a1d'; ctx.lineWidth = Math.max(1, u * 0.01);
    ctx.beginPath(); ctx.moveTo(X(0.42), R(-1.06)); ctx.quadraticCurveTo(X(0.58), R(-0.96), X(0.70), Y(-0.86 + nod)); ctx.stroke();
    // jambe : hanche -> genou -> cheville, botte dans l'étrier
    ctx.fillStyle = '#f8fafc';
    limb(ctx, X(-0.04), R(-1.10), X(0.15), R(-1.00), u * 0.09, u * 0.07);
    limb(ctx, X(0.15), R(-1.00), X(0.07), R(-0.88), u * 0.065, u * 0.05);
    ctx.fillStyle = '#111827';
    limb(ctx, X(0.075), R(-0.90), X(0.08), R(-0.80), u * 0.055, u * 0.045);
    ctx.fillRect(X(0.06), R(-0.80), u * 0.07, u * 0.02);
    ctx.fillStyle = '#9ca3af'; ctx.fillRect(X(0.05), R(-0.78), u * 0.08, u * 0.01); // étrier
    // buste (casaque) avec le numéro
    ctx.fillStyle = shade(ctx, 0, R(-1.28), 0, R(-1.06), [[0, j.shirt], [1, j.trim]]);
    ctx.beginPath();
    ctx.moveTo(X(-0.08), R(-1.08));
    ctx.quadraticCurveTo(X(-0.07), R(-1.24), X(0.10), R(-1.27));
    ctx.lineTo(X(0.30), R(-1.25));
    ctx.quadraticCurveTo(X(0.31), R(-1.15), X(0.22), R(-1.12));
    ctx.quadraticCurveTo(X(0.06), R(-1.06), X(-0.08), R(-1.08));
    ctx.fill();
    ctx.fillStyle = j.text; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    ctx.font = '900 ' + Math.max(7, u * 0.09) + 'px Inter, system-ui, sans-serif';
    ctx.fillText(String(p.player_number), X(0.08), R(-1.175));
    // bras vers l'encolure
    ctx.fillStyle = j.shirt;
    limb(ctx, X(0.25), R(-1.23), X(0.33), R(-1.13), u * 0.06, u * 0.05);
    limb(ctx, X(0.33), R(-1.13), X(0.42), R(-1.07), u * 0.05, u * 0.04);
    ctx.fillStyle = '#e5e7eb';
    ctx.beginPath(); ctx.arc(X(0.425), R(-1.065), u * 0.022, 0, Math.PI * 2); ctx.fill(); // gant
    // tête : visage, casque aux couleurs, lunettes
    ctx.fillStyle = '#8d5a3b';
    ctx.beginPath(); ctx.ellipse(X(0.355), R(-1.285), u * 0.045, u * 0.05, 0.3, 0, Math.PI * 2); ctx.fill();
    ctx.fillStyle = j.shirt;
    ctx.beginPath(); ctx.ellipse(X(0.345), R(-1.305), u * 0.06, u * 0.05, 0.25, Math.PI * 0.95, Math.PI * 2.1); ctx.fill();
    ctx.fillStyle = j.trim;
    ctx.beginPath(); ctx.moveTo(X(0.39), R(-1.31)); ctx.lineTo(X(0.44), R(-1.30)); ctx.lineTo(X(0.39), R(-1.29)); ctx.fill(); // visière
    ctx.fillStyle = '#111827'; ctx.fillRect(X(0.36), R(-1.285), u * 0.045, u * 0.016);       // lunettes
    ctx.restore();
  };

  function shadeDark(hex) {
    var n = parseInt(hex.slice(1), 16);
    var r = Math.round(((n >> 16) & 255) * 0.7), g = Math.round(((n >> 8) & 255) * 0.7), b = Math.round((n & 255) * 0.7);
    return 'rgb(' + r + ',' + g + ',' + b + ')';
  }

  Renderer.prototype._drawRace = function (t, raw, ts) {
    var L = this._layout();
    var parts = this.race.participants;
    var progress = this.progressAt(t);
    var leader = 0;
    Object.keys(progress).forEach(function (k) { leader = Math.max(leader, progress[k]); });
    this._drawTrack(L, leader * 2400);
    var size = Math.min(L.laneH * 1.05, this.w * 0.13);
    var finishMs = this.race.race_script.finish_ms;
    var elapsed = raw * this.race.race_script.duration_ms;
    var ranks = this._ranks();
    var self = this;
    parts.forEach(function (p, lane) {
      var pr = progress[String(p.player_number)] || 0;
      var x = L.startX + size * 0.6 + pr * (L.finishX - L.startX - size * 0.6);
      var cy = L.top + lane * L.laneH + L.laneH * 0.55;
      var finished = elapsed >= finishMs[String(p.player_number)];
      // ~2,3 foulées par seconde ; après l'arrivée le cheval ralentit au petit galop
      var gait = self.reducedMotion ? null : (finished ? ts / 1000 * 1.2 : ts / 1000 * 2.3) + lane * 0.17;
      self._drawHorseSide(p, lane, x, cy, size, gait);
      if (finished && ranks[p.player_number]) self._badge(x + 6, cy - size * 0.4, ranks[p.player_number], size);
      if (self.highlight && self.highlight.indexOf(p.player_number) >= 0) self._marker(x, cy - size * 0.55, size);
    });
  };

  Renderer.prototype._ranks = function () {
    var ranks = {};
    (this.race.results || []).forEach(function (r) { ranks[r.player_number] = r.position; });
    return ranks;
  };

  Renderer.prototype._badge = function (x, y, position, size) {
    var ctx = this.ctx, r = Math.max(9, size * 0.16);
    ctx.fillStyle = position === 1 ? '#f59e0b' : position === 2 ? '#cbd5e1' : position === 3 ? '#b45309' : '#334155';
    ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2); ctx.fill();
    ctx.fillStyle = position <= 2 ? '#0f172a' : '#ffffff';
    ctx.font = '800 ' + Math.round(r * 1.1) + 'px Inter, system-ui, sans-serif';
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    ctx.fillText(String(position), x, y + 1);
  };

  /* ---------------- Arrivée : classement ---------------- */
  Renderer.prototype._drawResults = function (ts) {
    var ctx = this.ctx, w = this.w, h = this.h;
    this._drawSky();
    var results = this.race.results;
    if (!results) return this._drawMessage('Résultat en attente…');
    var parts = {};
    this.race.participants.forEach(function (p, i) { parts[p.player_number] = { p: p, lane: i }; });
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    ctx.fillStyle = '#fbbf24';
    ctx.font = '800 ' + Math.round(clamp(w / 26, 16, 34)) + 'px Inter, system-ui, sans-serif';
    ctx.fillText('ARRIVÉE — COURSE #' + this.race.race_number, w / 2, h * 0.08);
    this._drawLogo(w * 0.015, h * 0.02, Math.min(h * 0.2, w * 0.13));

    // podium : gagnant au centre, de dos, mis en valeur
    var order = [results[1], results[0], results[2]];
    var heights = [0.16, 0.22, 0.10];
    var colW = w / 3.4;
    var self = this;
    order.forEach(function (r, i) {
      if (!r) return;
      var info = parts[r.player_number];
      var cx = w / 2 + (i - 1) * colW;
      var podiumTop = h * (0.78 - heights[i]);
      ctx.fillStyle = r.position === 1 ? '#f59e0b' : r.position === 2 ? '#94a3b8' : '#b45309';
      ctx.fillRect(cx - colW * 0.42, podiumTop, colW * 0.84, h * 0.78 - podiumTop + 2);
      ctx.fillStyle = '#0f172a';
      ctx.font = '800 ' + Math.round(clamp(w / 24, 14, 40)) + 'px Inter, system-ui, sans-serif';
      ctx.fillText(String(r.position), cx, podiumTop + (h * 0.78 - podiumTop) / 2);
      // le cavalier (casque compris) doit tenir entre le titre et le podium
      var room = podiumTop - h * 0.16;
      var size = Math.min(colW * 0.75, room / 1.08) * (r.position === 1 ? 1 : 0.9);
      var jump = r.position === 1 && !self.reducedMotion ? Math.abs(Math.sin(ts / 260)) * h * 0.03 : 0;
      if (r.position === 1) {
        var glow = ctx.createRadialGradient(cx, podiumTop - size * 0.55, 4, cx, podiumTop - size * 0.55, size * 0.8);
        glow.addColorStop(0, 'rgba(251,191,36,0.45)'); glow.addColorStop(1, 'rgba(251,191,36,0)');
        ctx.fillStyle = glow; ctx.fillRect(cx - size, podiumTop - size * 1.4, size * 2, size * 1.6);
      }
      self._drawRiderBack(info.p, info.lane, cx, podiumTop - jump, size);
    });

    // classement complet
    ctx.font = '600 ' + Math.round(clamp(w / 52, 10, 16)) + 'px Inter, system-ui, sans-serif';
    ctx.fillStyle = '#e2e8f0';
    var line = results.map(function (r) { return r.position + '. №' + r.player_number + ' ' + r.player_name; }).join('   ');
    ctx.fillText(line, w / 2, h * 0.9, w - 20);
  };

  /* ------------------------------------------------------------------
   * Temps réel : WebSocket existant (/ws/draws/all) + relecture périodique
   * ------------------------------------------------------------------ */
  function connectLive(onRace, onStatus) {
    var ws = null, attempts = 0, closed = false;
    function open() {
      var proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
      try { ws = new WebSocket(proto + location.host + '/ws/draws/all'); } catch (e) { return retry(); }
      ws.onopen = function () { attempts = 0; onStatus && onStatus(true); };
      ws.onmessage = function (ev) {
        try {
          var msg = JSON.parse(ev.data);
          if (msg.type === 'horse_race' && msg.data) onRace(msg.data, msg.event);
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
   * État en direct : course affichée (en piste) et course qui prend les
   * paris (souvent la suivante). Garde l'arrivée à l'écran quelques
   * secondes avant de passer à la course suivante.
   * ------------------------------------------------------------------ */
  var RESULT_HOLD_MS = 15000;

  function LiveState(onChange) {
    this.display = null;
    this.betting = null;
    this.holdUntil = 0;
    this.onChange = onChange || function () {};
  }

  LiveState.prototype._same = function (a, b) { return a && b && a.race_id === b.race_id; };

  LiveState.prototype._holding = function () {
    return this.display && Date.now() < this.holdUntil;
  };

  LiveState.prototype.applySnapshot = function (state) {
    var race = state.race, open = state.betting_race;
    this.betting = open || null;
    if (this._holding()) {
      if (race && this._same(race, this.display)) this.display = race;
    } else if (race && (race.status === 'RUNNING' || !this.display || !this._same(race, this.display) || race.status !== this.display.status)) {
      this.display = race;
    } else if (!race) {
      this.display = open || null;
    }
    this.onChange(this.display, this.betting);
  };

  LiveState.prototype.applyEvent = function (race, event) {
    var s = race.status;
    if (s === 'BETTING_OPEN' || s === 'SCHEDULED') {
      this.betting = s === 'BETTING_OPEN' ? race : this.betting;
      if (!this.display || (!this._holding() && this.display.status !== 'RUNNING' && this.display.status !== 'BETTING_CLOSED')) {
        this.display = race;
      }
    } else if (s === 'BETTING_CLOSED' || s === 'RUNNING') {
      if (this._same(this.betting, race)) this.betting = null;
      if (s === 'RUNNING' || !this.display || this._same(this.display, race) || !this._holding()) this.display = race;
    } else if (s === 'FINISHED' || s === 'SETTLED' || s === 'CANCELLED') {
      if (this._same(this.betting, race)) this.betting = null;
      if (this._same(this.display, race)) {
        if (!this.holdUntil || this.holdUntil < Date.now() - RESULT_HOLD_MS) this.holdUntil = Date.now() + RESULT_HOLD_MS;
        this.display = race;
      }
    }
    this.onChange(this.display, this.betting);
  };

  /** À appeler régulièrement : fin de l'affichage de l'arrivée -> course suivante. */
  LiveState.prototype.tick = function () {
    if (this.display && this.holdUntil && Date.now() >= this.holdUntil &&
        ['FINISHED', 'SETTLED', 'CANCELLED'].indexOf(this.display.status) >= 0) {
      this.holdUntil = 0;
      if (this.betting) { this.display = this.betting; this.onChange(this.display, this.betting); return true; }
    }
    return false;
  };

  global.HorseRaces = {
    LiveState: LiveState,
    Renderer: Renderer,
    ServerClock: ServerClock,
    formatCountdown: formatCountdown,
    statusLabel: function (s) { return STATUS_LABELS[s] || s; },
    jersey: function (lane) { return JERSEYS[lane % JERSEYS.length]; },
    connectLive: connectLive,
    parseUtc: parseUtc
  };
})(window);
