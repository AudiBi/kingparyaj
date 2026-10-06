/* app/static/js/thermal_receipt.js
 * Reçus King Paryaj pour imprimante THERMIQUE (80 mm ou 58 mm).
 *
 * - noir et blanc pur, gros caractères, contrastes francs (pas de gris clair
 *   ni d'ombre : une tête thermique ne les rend pas) ;
 * - largeur réglée sur le papier (@page) : 80 mm (72 mm imprimables) ou 58 mm (48 mm),
 *   hauteur = longueur du reçu (coupe juste après le texte) ;
 * - numéro de ticket en grand + code-barres Code 128 (lisible par une douchette) ;
 * - impression dans un cadre caché : pas de fenêtre pop-up bloquée.
 *
 * Usage : KPReceipt.print(spec) ; KPReceipt.html(spec) pour l'aperçu.
 */
(function (global) {
  'use strict';

  var C128 = ["11011001100","11001101100","11001100110","10010011000","10010001100","10001001100","10011001000","10011000100","10001100100","11001001000","11001000100","11000100100","10110011100","10011011100","10011001110","10111001100","10011101100","10011100110","11001110010","11001011100","11001001110","11011100100","11001110100","11101101110","11101001100","11100101100","11100100110","11101100100","11100110100","11100110010","11011011000","11011000110","11000110110","10100011000","10001011000","10001000110","10110001000","10001101000","10001100010","11010001000","11000101000","11000100010","10110111000","10110001110","10001101110","10111011000","10111000110","10001110110","11101110110","11010001110","11000101110","11011101000","11011100010","11011101110","11101011000","11101000110","11100010110","11101101000","11101100010","11100011010","11101111010","11001000010","11110001010","10100110000","10100001100","10010110000","10010000110","10000101100","10000100110","10110010000","10110000100","10011010000","10011000010","10000110100","10000110010","11000010010","11001010000","11110111010","11000010100","10001111010","10100111100","10010111100","10010011110","10111100100","10011110100","10011110010","11110100100","11110010100","11110010010","11011011110","11011110110","11110110110","10101111000","10100011110","10001011110","10111101000","10111100010","11110101000","11110100010","10111011110","10111101110","11101011110","11110101110","11010000100","11010010000","11010011100","11000111010"];
  var PAPER_KEY = 'kp_receipt_paper';

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function money(x) {
    if (x == null || isNaN(x)) return '—';
    return Number(x).toLocaleString('fr-FR', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + ' HTG';
  }
  function dateTime(value) {
    var d = value ? new Date(value) : new Date();
    if (isNaN(d)) d = new Date();
    return d.toLocaleDateString('fr-FR') + ' ' + d.toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  }

  /* ---------- Papier ---------- */
  function paper() {
    try { return localStorage.getItem(PAPER_KEY) === '58' ? 58 : 80; } catch (e) { return 80; }
  }
  function setPaper(v) {
    try { localStorage.setItem(PAPER_KEY, String(v === 58 || v === '58' ? 58 : 80)); } catch (e) { /* sans stockage : 80 mm */ }
  }
  /* Sélecteur à placer à côté du bouton Imprimer */
  function paperSelect(id) {
    var p = paper();
    return '<select id="' + (id || 'kpPaper') + '" class="kp-paper" aria-label="Largeur du papier" ' +
      'onchange="KPReceipt.setPaper(this.value)" style="padding:6px 8px;border:1px solid #cbd5e1;border-radius:8px;font-size:13px;">' +
      '<option value="80"' + (p === 80 ? ' selected' : '') + '>Papier 80 mm</option>' +
      '<option value="58"' + (p === 58 ? ' selected' : '') + '>Papier 58 mm</option></select>';
  }

  /* ---------- Code-barres Code 128 (jeu B) ---------- */
  function barcodeSvg(text, heightMm) {
    text = String(text || '').replace(/[^\x20-\x7E]/g, '');
    if (!text) return '';
    var codes = [104], sum = 104;
    for (var i = 0; i < text.length; i++) {
      var v = text.charCodeAt(i) - 32;
      codes.push(v);
      sum += v * (i + 1);
    }
    codes.push(sum % 103);
    var bits = codes.map(function (c) { return C128[c]; }).join('') + C128[106] + '11';
    var quiet = 10, width = bits.length + quiet * 2, rects = '', x = 0;
    while (x < bits.length) {
      if (bits[x] === '1') {
        var start = x;
        while (x < bits.length && bits[x] === '1') x++;
        rects += '<rect x="' + (start + quiet) + '" y="0" width="' + (x - start) + '" height="40"/>';
      } else x++;
    }
    return '<svg class="bc" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ' + width + ' 40" preserveAspectRatio="none" ' +
      'style="height:' + (heightMm || 12) + 'mm" shape-rendering="crispEdges" aria-label="Code-barres ' + esc(text) + '">' +
      '<rect width="100%" height="100%" fill="#fff"/><g fill="#000">' + rects + '</g></svg>';
  }

  /* ---------- Feuille de style (imprimante thermique) ---------- */
  function css(mm) {
    var printable = mm === 58 ? 48 : 72;
    var base = mm === 58 ? 11 : 12.5;
    return '' +
      '@page{size:' + mm + 'mm 297mm;margin:0}' +
      '*{box-sizing:border-box;margin:0;padding:0;-webkit-print-color-adjust:exact;print-color-adjust:exact}' +
      'html,body{background:#fff;color:#000}' +
      'body{width:' + mm + 'mm;padding:3mm ' + ((mm - printable) / 2) + 'mm 6mm;font:600 ' + base + 'px/1.35 "Arial Narrow",Arial,Helvetica,sans-serif}' +
      '.rc{width:' + printable + 'mm}' +
      '.logo{display:block;width:' + (mm === 58 ? 30 : 40) + 'mm;height:auto;margin:0 auto 1.5mm;filter:grayscale(1) contrast(1.6)}' +
      '.c{text-align:center}.shop{font-size:' + (base + 2) + 'px;font-weight:900;text-transform:uppercase;letter-spacing:.3px}' +
      '.small{font-size:' + (base - 1.5) + 'px;font-weight:600}' +
      '.band{background:#000;color:#fff;text-align:center;font-weight:900;font-size:' + (base + 9) + 'px;letter-spacing:2px;padding:1.2mm 0;margin:2mm 0 1.5mm}' +
      '.sub{text-align:center;font-weight:800;font-size:' + (base + 1) + 'px}' +
      '.sep{border:0;border-top:1.5px dashed #000;margin:2mm 0}' +
      '.sep2{border:0;border-top:2.5px solid #000;margin:2mm 0}' +
      '.row{display:flex;align-items:baseline;gap:1mm}.row .l{white-space:nowrap}.row .d{flex:1;border-bottom:1.5px dotted #000;transform:translateY(-1mm)}' +
      '.row .v{white-space:nowrap;font-weight:800;text-align:right}' +
      '.tk{border:2.5px solid #000;text-align:center;padding:1.3mm 0 1mm;margin:1.5mm 0}' +
      '.tk .t{font-size:' + (base - 1) + 'px;font-weight:800;letter-spacing:1px}' +
      '.tk .n{font:900 ' + (mm === 58 ? 17 : 21) + 'px/1.15 "Courier New",monospace;letter-spacing:1px}' +
      '.bc{display:block;width:100%;margin:1mm 0 .5mm}' +
      '.lbl{font-weight:800;margin:1.5mm 0 1mm}' +
      '.nums{display:flex;flex-wrap:wrap;gap:1.2mm}' +
      '.num{min-width:' + (mm === 58 ? 7 : 8.2) + 'mm;height:' + (mm === 58 ? 7 : 8.2) + 'mm;border:2px solid #000;border-radius:50%;display:flex;align-items:center;justify-content:center;' +
        'font:900 ' + (base + 1) + 'px "Courier New",monospace}' +
      '.num.on{background:#000;color:#fff}' +
      '.total{display:flex;justify-content:space-between;align-items:baseline;font-weight:900;font-size:' + (base + 5) + 'px;margin:1mm 0}' +
      '.stamp{border:3px double #000;text-align:center;font-weight:900;font-size:' + (base + 6) + 'px;letter-spacing:2px;padding:1mm 0;margin:2mm 0}' +
      '.stamp.won{background:#000;color:#fff;border-style:solid}' +
      '.mono{font:600 ' + (base - 2.5) + 'px/1.3 "Courier New",monospace;word-break:break-all}' +
      '.foot{text-align:center;font-size:' + (base - 1.5) + 'px;font-weight:700;margin-top:1.5mm}' +
      '.cut{text-align:center;font-size:' + (base - 2) + 'px;margin-top:3mm;letter-spacing:1px}';
  }

  /* ---------- Contenu ---------- */
  function row(label, value) {
    return '<div class="row"><span class="l">' + esc(label) + '</span><span class="d"></span><span class="v">' + value + '</span></div>';
  }

  function body(spec) {
    var h = '<div class="rc">';
    h += '<img class="logo" src="/static/img/logo-print.png" alt="King Paryaj">';
    h += '<div class="c shop">' + esc(spec.bureau || 'King Paryaj') + '</div>';
    if (spec.address || spec.phone) h += '<div class="c small">' + esc([spec.address, spec.phone].filter(Boolean).join(' · ')) + '</div>';
    h += '<div class="band">' + esc(spec.game) + '</div>';
    if (spec.round) h += '<div class="sub">' + esc(spec.round) + '</div>';
    if (spec.roundTime) h += '<div class="c small">' + esc(spec.roundTime) + '</div>';

    var number = spec.ticket || spec.code;
    if (number) {
      h += '<div class="tk"><div class="t">' + (spec.ticket ? 'TICKET N°' : 'CODE DU PARI') + '</div>' +
        '<div class="n">' + esc(spec.ticket || String(spec.code).slice(0, 8).toUpperCase()) + '</div>' +
        barcodeSvg(spec.ticket || spec.code, 11) + '</div>';
    }

    (spec.sections || []).forEach(function (s) {
      if (s.type === 'sep') { h += '<hr class="sep">'; return; }
      if (s.type === 'numbers') {
        var on = {};
        (s.highlight || []).forEach(function (n) { on[n] = true; });
        h += '<div class="lbl">' + esc(s.label) + '</div><div class="nums">' +
          (s.items || []).map(function (n) { return '<span class="num' + (on[n] ? ' on' : '') + '">' + esc(n) + '</span>'; }).join('') + '</div>';
        return;
      }
      if (s.type === 'image') {
        h += '<div class="c" style="margin:1.5mm 0"><img src="' + esc(s.src) + '" alt="" style="width:' + (s.mm || 32) + 'mm;height:auto;image-rendering:pixelated"></div>';
        if (s.caption) h += '<div class="c small">' + esc(s.caption) + '</div>';
        return;
      }
      if (s.type === 'text') { h += '<div style="margin:1mm 0;font-weight:800">' + esc(s.text) + '</div>'; return; }
      if (s.type === 'rows') { h += (s.rows || []).map(function (r) { return row(r[0], r[1]); }).join(''); return; }
      if (s.type === 'total') { h += '<hr class="sep2"><div class="total"><span>' + esc(s.label) + '</span><span>' + s.value + '</span></div>'; return; }
      if (s.type === 'stamp') { h += '<div class="stamp' + (s.won ? ' won' : '') + '">' + esc(s.text) + '</div>'; return; }
    });

    h += '<hr class="sep">';
    var info = [];
    if (spec.agent) info.push(['Agent', esc(spec.agent)]);
    info.push(['Émis le', esc(dateTime(spec.placedAt))]);
    if (spec.ticket && spec.code) info.push(['Réf. pari', esc(String(spec.code).slice(0, 8).toUpperCase())]);
    h += info.map(function (r) { return row(r[0], r[1]); }).join('');
    if (spec.hash) {
      h += '<div class="lbl" style="margin-top:2mm">Empreinte du tirage (SHA-256)</div><div class="mono">' + esc(spec.hash) + '</div>';
    }
    if (spec.seed) h += '<div class="lbl">Seed révélé</div><div class="mono">' + esc(spec.seed) + '</div>';
    if (spec.verifyUrl) h += '<div class="lbl">Vérification</div><div class="mono">' + esc(spec.verifyUrl) + '</div>';
    h += '<hr class="sep">';
    h += '<div class="foot">' + esc(spec.footer || 'Gain payable le jour même, avant minuit, sur présentation de ce reçu.') + '<br>' +
      'Jeu réservé aux personnes de 18 ans et plus.<br>Jouez de façon responsable.</div>';
    h += '<div class="foot" style="font-size:13px;font-weight:900;margin-top:2mm">MERCI ET BONNE CHANCE !</div>';
    h += '<div class="cut">- - - - - - - - - - - - - - - - - - - -</div>';
    return h + '</div>';
  }

  function html(spec, mm) {
    mm = mm || paper();
    return '<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8"><title>Reçu ' + esc(spec.game) + '</title><style>' + css(mm) +
      '</style></head><body>' + body(spec) + '</body></html>';
  }

  /* Aperçu dans la page (iframe à la largeur du papier) */
  function preview(container, spec) {
    var mm = paper();
    container.innerHTML = '';
    var frame = document.createElement('iframe');
    frame.title = 'Aperçu du reçu';
    frame.style.cssText = 'width:' + mm + 'mm;max-width:100%;border:1px solid #e2e8f0;background:#fff;display:block;margin:0 auto;height:120mm';
    container.appendChild(frame);
    frame.srcdoc = html(spec, mm);
    frame.onload = function () {
      try { frame.style.height = (frame.contentDocument.documentElement.scrollHeight + 4) + 'px'; } catch (e) { /* aperçu seulement */ }
    };
  }

  /* Hauteur de page = longueur du reçu : le rouleau thermique est coupé juste
     après le texte (pas de page A4 / Letter, pas de papier gaspillé). */
  function fitPage(doc) {
    try {
      var mm = paper();
      var heightMm = Math.ceil(doc.documentElement.scrollHeight * 25.4 / 96) + 4;
      var st = doc.createElement('style');
      st.textContent = '@page{size:' + mm + 'mm ' + heightMm + 'mm;margin:0}';
      doc.head.appendChild(st);
    } catch (e) { /* taille par défaut */ }
  }

  /* Impression (cadre caché, attend le logo) */
  function print(spec) {
    var frame = document.createElement('iframe');
    frame.setAttribute('aria-hidden', 'true');
    frame.style.cssText = 'position:fixed;right:0;bottom:0;width:0;height:0;border:0;visibility:hidden';
    document.body.appendChild(frame);
    var doc = frame.contentWindow.document;
    doc.open(); doc.write(html(spec)); doc.close();
    var done = false;
    function go() {
      if (done) return;
      done = true;
      fitPage(doc);
      try { frame.contentWindow.focus(); frame.contentWindow.print(); } catch (e) { /* impression annulée */ }
      setTimeout(function () { frame.remove(); }, 60000);
    }
    var imgs = Array.prototype.slice.call(doc.images || []).filter(function (i) { return !i.complete; });
    if (!imgs.length) return setTimeout(go, 50);
    var left = imgs.length;
    imgs.forEach(function (i) { i.onload = i.onerror = function () { if (--left === 0) go(); }; });
    setTimeout(go, 2500);
  }

  global.KPReceipt = {
    print: print, html: html, preview: preview, fitPage: fitPage, barcodeSvg: barcodeSvg,
    paper: paper, setPaper: setPaper, paperSelect: paperSelect, money: money, dateTime: dateTime
  };
})(window);
