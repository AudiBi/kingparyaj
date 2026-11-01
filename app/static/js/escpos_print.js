/* app/static/js/escpos_print.js
 * Impression DIRECTE des reçus en ESC/POS (imprimantes thermiques Xprinter XP-80C et compatibles),
 * via QZ Tray (petit programme gratuit installé sur le poste : https://qz.io/download/).
 *
 * Pourquoi : en passant par la fenêtre d'impression du navigateur, la longueur du ticket est imposée
 * par le pilote Windows (ex. 72.1 x 210 mm : beaucoup de blanc). En ESC/POS on envoie les commandes
 * natives de l'imprimante : longueur exacte, aucune marge, coupe automatique.
 *
 * Le contenu vient du même objet `spec` que le reçu imprimé par le navigateur (thermal_receipt.js) :
 *   { game, bureau, address, phone, round, roundTime, ticket, code, agent, placedAt, sections:[…] }
 *
 * Réglages (par poste, dans le navigateur) : mode « escpos » ou « browser », nom de l'imprimante.
 * Si l'impression directe échoue (QZ Tray absent, imprimante introuvable…), on retombe sur la
 * fenêtre d'impression du navigateur : un ticket n'est jamais perdu.
 *
 * Les fonctions de construction (build, toBytes, toText) sont pures : elles se testent sous Node.
 */
(function (root) {
  'use strict';

  var MODE_KEY = 'kp_print_mode';
  var PRINTER_KEY = 'kp_printer_name';
  var QZ_SRC = '/static/js/vendor/qz-tray.js';

  /* ---------- Jeu de caractères : CP858 (ESC t 19), accents français ---------- */
  var CP858 = {
    'Ç': 0x80, 'ü': 0x81, 'é': 0x82, 'â': 0x83, 'ä': 0x84, 'à': 0x85, 'å': 0x86, 'ç': 0x87, 'ê': 0x88, 'ë': 0x89,
    'è': 0x8A, 'ï': 0x8B, 'î': 0x8C, 'ì': 0x8D, 'Ä': 0x8E, 'Å': 0x8F, 'É': 0x90, 'ô': 0x93, 'ö': 0x94, 'ò': 0x95,
    'û': 0x96, 'ù': 0x97, 'ÿ': 0x98, 'Ö': 0x99, 'Ü': 0x9A, '£': 0x9C, '«': 0xAE, '»': 0xAF, 'Â': 0xB6, 'À': 0xB7,
    'Ê': 0xD2, 'Ë': 0xD3, 'È': 0xD4, '€': 0xD5, 'Î': 0xD7, 'Ï': 0xD8, 'Ô': 0xE2, 'Û': 0xEA, 'Ù': 0xEB,
    '°': 0xF8, '·': 0xFA
  };
  var SUBSTITUTES = {
    'œ': 'oe', 'Œ': 'OE', '’': "'", '‘': "'", '“': '"', '”': '"', '–': '-', '—': '-', '→': '>', '←': '<',
    '…': '...', '×': 'x', ' ': ' ', ' ': ' ', '•': '*'
  };

  function plain(s) {
    return String(s == null ? '' : s)
      .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, '&');
  }
  /* Texte -> caractères que l'imprimante sait écrire (le reste devient « ? ») */
  function norm(s) {
    var out = '';
    plain(s).split('').forEach(function (c) {
      if (CP858[c] !== undefined || (c >= ' ' && c <= '~')) out += c;
      else if (SUBSTITUTES[c] !== undefined) out += SUBSTITUTES[c];
      else out += '?';
    });
    return out;
  }
  function enc(s) {
    var bytes = [];
    norm(s).split('').forEach(function (c) { bytes.push(CP858[c] !== undefined ? CP858[c] : c.charCodeAt(0)); });
    return bytes;
  }

  /* ---------- Mise en page texte ---------- */
  function wrap(text, width) {
    var lines = [], line = '';
    norm(text).split(/\s+/).forEach(function (word) {
      while (word.length > width) {  // mot plus long qu'une ligne : coupé
        if (line) { lines.push(line); line = ''; }
        lines.push(word.slice(0, width)); word = word.slice(width);
      }
      if (!word) return;
      if (!line) line = word;
      else if ((line + ' ' + word).length <= width) line += ' ' + word;
      else { lines.push(line); line = word; }
    });
    if (line) lines.push(line);
    return lines;
  }
  function padCenter(s, w) {
    var left = Math.max(0, Math.floor((w - s.length) / 2));
    var out = new Array(left + 1).join(' ') + s;
    return out + new Array(Math.max(0, w - out.length) + 1).join(' ');
  }
  function twoColumns(left, right, width) {
    left = norm(left); right = norm(right);
    var gap = width - left.length - right.length;
    if (gap >= 1) return [left + new Array(gap + 1).join(' ') + right];
    return wrap(left, width).concat([new Array(Math.max(0, width - right.length) + 1).join(' ') + right.slice(-width)]);
  }
  function dateTime(value) {
    var d = value ? new Date(value) : new Date();
    if (isNaN(d)) d = new Date();
    return d.toLocaleDateString('fr-FR') + ' ' + d.toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  }

  /* ---------- Construction du ticket : liste d'opérations ---------- */
  /* opts : { columns: 48 | 32, logo: bitmap | null, images: { index: bitmap } } */
  function build(spec, opts) {
    opts = opts || {};
    var cols = opts.columns || 48;
    var ops = [];
    function line(text, style) {
      var s = style || {};
      var width = s.dw ? Math.floor(cols / 2) : cols;
      wrap(text, width).forEach(function (t) { ops.push({ t: 'line', text: t, style: s }); });
    }
    function raw(text, style) { ops.push({ t: 'line', text: norm(text), style: style || {} }); }

    if (opts.logo) ops.push({ t: 'raster', bitmap: opts.logo });
    if (spec.bureau) line(spec.bureau, { align: 'c', bold: true });
    var contact = [spec.address, spec.phone].filter(Boolean).join(' · ');
    if (contact) line(contact, { align: 'c' });

    var dw = Math.floor(cols / 2);
    raw(padCenter(norm(spec.game || '').slice(0, dw), dw), { align: 'c', dw: true, dh: true, inv: true, bold: true });
    if (spec.round) line(spec.round, { align: 'c', bold: true });
    if (spec.roundTime) line(spec.roundTime, { align: 'c' });
    line('Date : ' + dateTime(spec.placedAt), { align: 'c', bold: true });

    var number = spec.ticket || spec.code;
    if (number) {
      var shown = norm(spec.ticket || String(spec.code).slice(0, 8).toUpperCase());
      ops.push({ t: 'sep', char: '-' });
      raw(shown, { align: 'c', bold: true, dw: shown.length <= dw, dh: shown.length <= dw });
      ops.push({ t: 'barcode', data: String(spec.ticket || spec.code) });
    }
    ops.push({ t: 'sep', char: '-' });

    (spec.sections || []).forEach(function (s, index) {
      if (s.type === 'sep') { ops.push({ t: 'sep', char: '-' }); return; }
      if (s.type === 'numbers') {
        line(s.label, { bold: true });
        var on = {};
        (s.highlight || []).forEach(function (n) { on[n] = true; });
        var perLine = Math.max(1, Math.floor(dw / 4));
        var items = (s.items || []).map(function (n) {
          var t = norm(n), hit = !!on[n];
          var cell = hit ? ' ' + t + ' ' : '[' + t + ']';          // numéro trouvé : blanc sur noir
          while (cell.length < 4) cell += ' ';
          return { text: cell.slice(0, 4), inv: hit };
        });
        for (var i = 0; i < items.length; i += perLine) ops.push({ t: 'cells', items: items.slice(i, i + perLine) });
        return;
      }
      if (s.type === 'image') {
        if (opts.images && opts.images[index]) ops.push({ t: 'raster', bitmap: opts.images[index] });
        if (s.caption) line(s.caption, { align: 'c' });
        return;
      }
      if (s.type === 'text') { line(s.text, { bold: true }); return; }
      if (s.type === 'rows') {
        (s.rows || []).forEach(function (r) { twoColumns(r[0], plain(r[1]), cols).forEach(function (t) { raw(t, {}); }); });
        return;
      }
      if (s.type === 'total') {
        ops.push({ t: 'sep', char: '=' });
        twoColumns(s.label, plain(s.value), cols).forEach(function (t) { raw(t, { bold: true, dh: true }); });
        return;
      }
      if (s.type === 'stamp') {
        raw(padCenter(norm(s.text).slice(0, dw), dw), { align: 'c', dw: true, dh: true, bold: true, inv: !!s.won });
      }
    });
    return ops;
  }

  /* ---------- Opérations -> octets ESC/POS ---------- */
  function toBytes(ops, opts) {
    opts = opts || {};
    var cols = opts.columns || 48;
    var out = [0x1B, 0x40, 0x1B, 0x74, 19];               // ESC @ (init), ESC t 19 (CP858)
    var ALIGN = { l: 0, c: 1, r: 2 };
    function style(s) {
      s = s || {};
      out.push(0x1B, 0x61, ALIGN[s.align || 'l']);                       // alignement
      out.push(0x1B, 0x45, s.bold ? 1 : 0);                              // gras
      out.push(0x1D, 0x21, (s.dw ? 0x10 : 0) | (s.dh ? 0x01 : 0));       // taille (largeur / hauteur ×2)
      out.push(0x1D, 0x42, s.inv ? 1 : 0);                               // inversé (blanc sur noir)
    }
    ops.forEach(function (op) {
      if (op.t === 'line') {
        style(op.style);
        Array.prototype.push.apply(out, enc(op.text));
        out.push(0x0A);
      } else if (op.t === 'sep') {
        style({});
        out.push.apply(out, enc(new Array(cols + 1).join(op.char || '-')));
        out.push(0x0A);
      } else if (op.t === 'cells') {
        style({ bold: true, dw: true, dh: true });
        op.items.forEach(function (it) {
          out.push(0x1D, 0x42, it.inv ? 1 : 0);
          Array.prototype.push.apply(out, enc(it.text));
        });
        out.push(0x1D, 0x42, 0, 0x0A);
      } else if (op.t === 'barcode') {
        var data = '{B' + String(op.data).replace(/[^\x20-\x7E]/g, '');       // Code 128, jeu B
        var modules = 11 * (data.length + 1) + 13;
        style({ align: 'c' });
        out.push(0x1D, 0x68, 56);                                          // hauteur ≈ 7 mm
        out.push(0x1D, 0x77, modules * 2 <= cols * 12 - 6 ? 2 : 1);        // épaisseur des barres
        out.push(0x1D, 0x48, 0);                                           // pas de texte sous le code
        out.push(0x1D, 0x6B, 73, data.length);
        for (var i = 0; i < data.length; i++) out.push(data.charCodeAt(i));
        out.push(0x0A);
      } else if (op.t === 'raster') {
        var b = op.bitmap;
        style({ align: 'c' });
        out.push(0x1D, 0x76, 0x30, 0, b.bytesPerRow & 0xFF, b.bytesPerRow >> 8, b.height & 0xFF, b.height >> 8);
        for (var k = 0; k < b.data.length; k++) out.push(b.data[k]);
      }
    });
    style({});
    out.push(0x1D, 0x56, 66, 0);                                           // coupe partielle après avance au couteau
    return new Uint8Array(out);
  }

  /* ---------- Aperçu texte (essais, support) ---------- */
  function toText(ops, opts) {
    var cols = (opts && opts.columns) || 48;
    var lines = [];
    ops.forEach(function (op) {
      if (op.t === 'sep') { lines.push(new Array(cols + 1).join(op.char || '-')); return; }
      if (op.t === 'barcode') { lines.push('|||| ' + op.data + ' ||||'); return; }
      if (op.t === 'raster') { lines.push('[image ' + op.bitmap.bytesPerRow * 8 + 'x' + op.bitmap.height + ']'); return; }
      if (op.t === 'cells') { lines.push(op.items.map(function (i) { return i.text; }).join('')); return; }
      var s = op.style || {}, width = s.dw ? Math.floor(cols / 2) : cols, t = op.text;
      if (s.align === 'c') t = padCenter(t.replace(/\s+$/, ''), width).replace(/\s+$/, '');
      if (s.align === 'r') t = new Array(Math.max(0, width - t.length) + 1).join(' ') + t;
      lines.push(s.dw ? t.split('').join(' ').slice(0, cols) : t);
    });
    return lines.join('\n');
  }

  function toBase64(bytes) {
    var bin = '';
    for (var i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    return (typeof btoa === 'function') ? btoa(bin) : Buffer.from(bytes).toString('base64');
  }

  var core = { build: build, toBytes: toBytes, toText: toText, toBase64: toBase64, enc: enc, norm: norm, wrap: wrap };
  if (typeof module !== 'undefined' && module.exports) { module.exports = core; }
  if (typeof window === 'undefined') { return; }

  /* ====================================================================
   * Navigateur : image -> trame, QZ Tray, réglages
   * ==================================================================== */

  /* Image -> trame monochrome (GS v 0). `dots` : largeur voulue en points (8 points = 1 mm). */
  function rasterize(src, dots) {
    return new Promise(function (resolve, reject) {
      var img = new Image();
      img.onload = function () {
        try {
          var width = Math.max(8, Math.round(dots / 8) * 8);
          var height = Math.max(1, Math.round(img.naturalHeight * width / img.naturalWidth));
          var canvas = document.createElement('canvas');
          canvas.width = width; canvas.height = height;
          var ctx = canvas.getContext('2d');
          ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, width, height);
          ctx.drawImage(img, 0, 0, width, height);
          var pixels = ctx.getImageData(0, 0, width, height).data;
          var bytesPerRow = width / 8, data = new Uint8Array(bytesPerRow * height);
          for (var y = 0; y < height; y++) {
            for (var x = 0; x < width; x++) {
              var p = (y * width + x) * 4;
              var lum = 0.299 * pixels[p] + 0.587 * pixels[p + 1] + 0.114 * pixels[p + 2];
              if (lum < 140) data[y * bytesPerRow + (x >> 3)] |= 0x80 >> (x & 7);
            }
          }
          resolve({ bytesPerRow: bytesPerRow, height: height, data: data });
        } catch (e) { reject(e); }
      };
      img.onerror = function () { reject(new Error('image introuvable : ' + src)); };
      img.src = src;
    });
  }

  function store(key, value) { try { if (value == null) localStorage.removeItem(key); else localStorage.setItem(key, value); } catch (e) { /* sans stockage */ } }
  function read(key) { try { return localStorage.getItem(key); } catch (e) { return null; } }

  function enabled() { return read(MODE_KEY) === 'escpos'; }
  function columns() { return (root.KPReceipt && root.KPReceipt.paper() === 58) ? 32 : 48; }
  function printerName() { return read(PRINTER_KEY) || ''; }

  /* Délai maximum : sans QZ Tray, la connexion pourrait rester en attente très longtemps */
  function withTimeout(promise, ms, message) {
    return new Promise(function (resolve, reject) {
      var timer = setTimeout(function () { reject(new Error(message)); }, ms);
      promise.then(function (v) { clearTimeout(timer); resolve(v); }, function (e) { clearTimeout(timer); reject(e); });
    });
  }

  var qzLoading = null;
  function loadQz() {
    if (root.qz) return Promise.resolve(root.qz);
    if (!qzLoading) {
      qzLoading = new Promise(function (resolve, reject) {
        var s = document.createElement('script');
        s.src = QZ_SRC;
        s.onload = function () { resolve(root.qz); };
        s.onerror = function () { qzLoading = null; reject(new Error('bibliothèque QZ Tray introuvable')); };
        document.head.appendChild(s);
      });
    }
    return qzLoading;
  }
  function connect() {
    return loadQz().then(function (qz) {
      if (qz.websocket.isActive()) return qz;
      var unavailable = "QZ Tray n'est pas lancé sur ce poste (installez-le : https://qz.io/download/)";
      return withTimeout(qz.websocket.connect({ retries: 0, delay: 1 }), 6000, unavailable).then(function () { return qz; }, function () {
        throw new Error(unavailable);
      });
    });
  }
  function listPrinters() {
    return connect().then(function (qz) { return withTimeout(qz.printers.find(), 10000, "liste des imprimantes indisponible"); });
  }
  function resolvePrinter(qz) {
    var name = printerName();
    if (name) return Promise.resolve(name);
    return qz.printers.find('XP-80').then(function (found) {
      return Array.isArray(found) ? found[0] : found;
    }).catch(function () { return qz.printers.getDefault(); });
  }

  /* Trame du logo et des images, puis ticket complet */
  function prepare(spec) {
    var o = { columns: columns(), logo: null, images: {} };
    var dotsPerMm = 8, logoMm = o.columns === 32 ? 12 : 16;
    var jobs = [rasterize('/static/img/logo-print.png', logoMm * dotsPerMm).then(function (b) { o.logo = b; }, function () { /* sans logo */ })];
    (spec.sections || []).forEach(function (s, i) {
      if (s.type === 'image' && s.src) jobs.push(rasterize(s.src, (s.mm || 32) * dotsPerMm).then(function (b) { o.images[i] = b; }, function () { /* sans image */ }));
    });
    return Promise.all(jobs).then(function () { return o; });
  }

  function sendBytes(bytes) {
    return connect().then(function (qz) {
      return resolvePrinter(qz).then(function (name) {
        if (!name) throw new Error('aucune imprimante choisie');
        var config = qz.configs.create(name);
        return withTimeout(qz.print(config, [{ type: 'raw', format: 'command', flavor: 'base64', data: toBase64(bytes) }]),
          20000, "l'imprimante ne répond pas");
      });
    });
  }

  function print(spec) {
    return prepare(spec).then(function (o) {
      return sendBytes(toBytes(build(spec, o), o));
    });
  }

  /* Ticket de test (réglages) */
  function printTest() {
    return print({
      game: 'TEST', bureau: (root.KP_AGENT && root.KP_AGENT.bureau) || 'King Paryaj', round: 'Impression directe ESC/POS',
      ticket: 'TEST-0001-ABCD', placedAt: new Date().toISOString(),
      sections: [
        { type: 'numbers', label: 'Numéros joués (6)', items: [1, 12, 23, 34, 45, 56], highlight: [12, 45] },
        { type: 'sep' },
        { type: 'rows', rows: [['Mise', '50,00 HTG'], ['Gain maximum', '500,00 HTG']] },
        { type: 'total', label: 'TOTAL PAYÉ', value: '50,00 HTG' }
      ]
    });
  }

  /* ---------- Fenêtre de réglage ---------- */
  function settings() {
    var old = document.getElementById('kpEscposDialog');
    if (old) old.remove();
    var box = document.createElement('div');
    box.id = 'kpEscposDialog';
    box.style.cssText = 'position:fixed;inset:0;background:rgba(15,23,42,.55);z-index:100000;display:flex;align-items:center;justify-content:center;padding:16px;';
    box.innerHTML =
      '<div style="background:#fff;border-radius:12px;max-width:460px;width:100%;padding:18px;font:14px/1.4 system-ui,sans-serif;color:#0f172a;">' +
      '<h3 style="margin:0 0 10px;font-size:17px;">Imprimante des tickets</h3>' +
      '<label style="display:block;margin:6px 0;"><input type="radio" name="kpMode" value="escpos"> <b>Impression directe ESC/POS</b> (recommandée) — longueur exacte, sans marges, coupe automatique. Demande <a href="https://qz.io/download/" target="_blank" rel="noopener">QZ Tray</a> sur ce poste.</label>' +
      '<label style="display:block;margin:6px 0;"><input type="radio" name="kpMode" value="browser"> Fenêtre d\'impression du navigateur</label>' +
      '<div style="margin:10px 0 4px;font-weight:600;">Imprimante</div>' +
      '<select id="kpPrinterSel" style="width:100%;padding:7px;border:1px solid #cbd5e1;border-radius:8px;"></select>' +
      '<div id="kpEscposMsg" style="min-height:20px;margin:8px 0;font-size:13px;color:#475569;"></div>' +
      '<div style="display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end;">' +
      '<button type="button" id="kpDetect" style="padding:7px 12px;border:1px solid #cbd5e1;background:#f8fafc;border-radius:8px;cursor:pointer;">Détecter les imprimantes</button>' +
      '<button type="button" id="kpTest" style="padding:7px 12px;border:1px solid #cbd5e1;background:#f8fafc;border-radius:8px;cursor:pointer;">Ticket test</button>' +
      '<button type="button" id="kpSave" style="padding:7px 14px;border:0;background:#059669;color:#fff;border-radius:8px;cursor:pointer;">Enregistrer</button>' +
      '<button type="button" id="kpClose" style="padding:7px 12px;border:1px solid #cbd5e1;background:#fff;border-radius:8px;cursor:pointer;">Fermer</button>' +
      '</div></div>';
    document.body.appendChild(box);
    var $ = function (id) { return document.getElementById(id); };
    var msg = function (text, bad) { $('kpEscposMsg').textContent = text; $('kpEscposMsg').style.color = bad ? '#b91c1c' : '#475569'; };
    document.querySelectorAll('input[name="kpMode"]').forEach(function (r) { r.checked = r.value === (enabled() ? 'escpos' : 'browser'); });
    function fill(names) {
      var current = printerName();
      var list = names.slice();
      if (current && list.indexOf(current) < 0) list.unshift(current);
      $('kpPrinterSel').innerHTML = list.length ? list.map(function (n) { return '<option>' + n.replace(/</g, '&lt;') + '</option>'; }).join('')
        : '<option value="">(cliquez sur « Détecter les imprimantes »)</option>';
      if (current) $('kpPrinterSel').value = current;
    }
    fill([]);
    $('kpDetect').onclick = function () {
      msg('Recherche…');
      listPrinters().then(function (names) { fill(names); msg(names.length + ' imprimante(s) trouvée(s). Choisissez la XP-80C.'); })
        .catch(function (e) { msg(e.message, true); });
    };
    function save() {
      var mode = document.querySelector('input[name="kpMode"]:checked');
      store(MODE_KEY, mode && mode.value === 'escpos' ? 'escpos' : 'browser');
      store(PRINTER_KEY, $('kpPrinterSel').value || null);
    }
    $('kpTest').onclick = function () {
      store(PRINTER_KEY, $('kpPrinterSel').value || null);
      msg('Envoi du ticket test…');
      printTest().then(function () { msg('Ticket test envoyé.'); }).catch(function (e) { msg(e.message || String(e), true); });
    };
    $('kpSave').onclick = function () { save(); box.remove(); if (root.showToast) root.showToast('Réglage d\'impression enregistré', 'success'); };
    $('kpClose').onclick = function () { box.remove(); };
  }

  root.KPEscPos = {
    enabled: enabled, print: print, printTest: printTest, settings: settings, listPrinters: listPrinters,
    build: build, toBytes: toBytes, toText: toText, columns: columns, prepare: prepare
  };
})(typeof window !== 'undefined' ? window : globalThis);
