# tests/test_services/test_escpos_print.py
"""Impression directe ESC/POS : construction des commandes d'un ticket (exécutée sous Node)."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULE = (ROOT / "app" / "static" / "js" / "escpos_print.js").as_posix()

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node.js absent")

SCRIPT = r"""
const E = require(%r);
const spec = JSON.parse(process.argv[1]);
const columns = Number(process.argv[2]);
const ops = E.build(spec, { columns });
const bytes = Array.from(E.toBytes(ops, { columns }));
const textLines = E.toText(ops, { columns }).split('\n');
console.log(JSON.stringify({ bytes, textLines, ops: ops.map(o => o.t) }));
""" % MODULE

SPEC = {
    "game": "KENO", "bureau": "Bureau Pétion-Ville", "address": "12 rue Capois", "phone": "3700-0000",
    "round": "Tirage n° 5", "roundTime": "Tirage lancé par un agent", "ticket": "KNO-ABCD-1234", "code": "0b9f2c3e-1111",
    "placedAt": "2026-10-29T15:04:05Z",
    "sections": [
        {"type": "numbers", "label": "Numéros joués (7)", "items": [1, 12, 23, 34, 45, 56, 78], "highlight": [12, 45]},
        {"type": "sep"},
        {"type": "rows", "rows": [["Mise", "50,00 HTG"], ["Cheval", "N° 7 Crème &amp; Co"]]},
        {"type": "total", "label": "TOTAL PAYÉ", "value": "50,00 HTG"},
        {"type": "stamp", "text": "GAGNANT", "won": True},
    ],
}


def run(spec=SPEC, columns=48):
    out = subprocess.run(["node", "-e", SCRIPT, json.dumps(spec), str(columns)], capture_output=True, text=True, timeout=60, encoding="utf-8")
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def find(seq, pattern):
    n = len(pattern)
    return [i for i in range(len(seq) - n + 1) if seq[i:i + n] == pattern]


def test_ticket_starts_with_init_and_ends_with_cut():
    data = run()["bytes"]
    assert data[:5] == [0x1B, 0x40, 0x1B, 0x74, 19]            # init + jeu de caractères CP858
    assert data[-4:] == [0x1D, 0x56, 66, 0]                    # coupe partielle : aucun papier de plus après le couteau


def test_accents_use_the_printer_codepage():
    data = run()["bytes"]
    assert 0x82 in data                                        # é
    assert 0xD4 not in data and 0xF8 in data                   # ° de « n° »
    assert 0x90 in data                                        # É de « PAYÉ »


def test_entities_are_decoded_and_unknown_characters_replaced():
    result = run({**SPEC, "bureau": "Bureau ★ test", "sections": [{"type": "rows", "rows": [["Nom", "Crème &amp; Co"]]}]})
    text = "\n".join(result["textLines"])
    assert "Crème & Co" in text and "&amp;" not in text
    assert "Bureau ? test" in text                              # caractère non imprimable -> ?


@pytest.mark.parametrize("columns", [48, 32])
def test_no_line_is_wider_than_the_paper(columns):
    result = run(columns=columns)
    for line in result["textLines"]:
        if line.startswith("||||"):                             # aperçu du code-barres
            continue
        spaced = line.replace(" ", "")                          # les lignes en double largeur sont espacées dans l'aperçu
        assert len(line) <= columns or len(spaced) <= columns // 2, (columns, line)


def test_barcode_is_code128_with_the_ticket_number():
    data = run()["bytes"]
    payload = list(b"{BKNO-ABCD-1234")
    assert find(data, [0x1D, 0x6B, 73, len(payload)] + payload)  # GS k 73 : Code 128, jeu B
    assert find(data, [0x1D, 0x48, 0])                         # pas de texte sous le code


def test_found_numbers_are_printed_inverted_and_others_in_brackets():
    result = run()
    cells = [line for line in result["textLines"] if "[1]" in line]
    assert cells and " 12 " in cells[0] and "[23]" in cells[0] and " 45 " in cells[0]
    data = result["bytes"]
    assert find(data, [0x1D, 0x42, 1]) and find(data, [0x1D, 0x42, 0])  # inversé puis normal


def test_total_is_bold_and_double_height_and_won_stamp_is_inverted():
    data = run()["bytes"]
    assert find(data, [0x1B, 0x45, 1, 0x1D, 0x21, 0x01])       # gras + hauteur ×2 (total)
    assert find(data, [0x1D, 0x21, 0x11, 0x1D, 0x42, 1])       # largeur et hauteur ×2, inversé (GAGNANT)


def test_long_texts_wrap_and_rows_keep_value_right_aligned():
    long_row = {"type": "rows", "rows": [["Un libellé vraiment très très long qui dépasse", "1 234 567,00 HTG"]]}
    result = run({**SPEC, "sections": [long_row]})
    lines = [l for l in result["textLines"] if "HTG" in l or "libellé" in l]
    assert all(len(l) <= 48 for l in lines) and any(l.rstrip().endswith("1 234 567,00 HTG") for l in lines)


def test_ticket_without_number_has_no_barcode():
    result = run({**SPEC, "ticket": None, "code": None})
    assert "barcode" not in result["ops"]
