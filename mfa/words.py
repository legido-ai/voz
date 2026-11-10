"""Spanish number words -> digits, for a code that is read aloud.

The streaming ASR writes numbers as words ("quinientos diez mil ochocientos
veintinueve") and the one-shot model writes them as digits ("510.829"); the user may
read digit by digit ("cinco uno cero ocho dos nueve"). All three must reach the same
six-digit string, so the matcher works on two streams: literal digits and parsed words.
"""

from __future__ import annotations

import re
import unicodedata

_UNITS = {
    "cero": 0, "uno": 1, "un": 1, "una": 1, "dos": 2, "tres": 3, "cuatro": 4,
    "cinco": 5, "seis": 6, "siete": 7, "ocho": 8, "nueve": 9, "diez": 10,
    "once": 11, "doce": 12, "trece": 13, "catorce": 14, "quince": 15,
    "dieciseis": 16, "diecisiete": 17, "dieciocho": 18, "diecinueve": 19,
    "veinte": 20, "veintiuno": 21, "veintiun": 21, "veintidos": 22, "veintitres": 23,
    "veinticuatro": 24, "veinticinco": 25, "veintiseis": 26, "veintisiete": 27,
    "veintiocho": 28, "veintinueve": 29,
    "treinta": 30, "cuarenta": 40, "cincuenta": 50, "sesenta": 60, "setenta": 70,
    "ochenta": 80, "noventa": 90,
    "cien": 100, "ciento": 100, "doscientos": 200, "doscientas": 200,
    "trescientos": 300, "trescientas": 300, "cuatrocientos": 400, "cuatrocientas": 400,
    "quinientos": 500, "quinientas": 500, "seiscientos": 600, "seiscientas": 600,
    "setecientos": 700, "setecientas": 700, "ochocientos": 800, "ochocientas": 800,
    "novecientos": 900, "novecientas": 900,
}
_SCALES = {"mil": 1000, "millon": 1_000_000, "millones": 1_000_000}
_GLUE = {"y", "con"}

_TOKEN = re.compile(r"\d+|[a-z]+")
_DIGITS = re.compile(r"\d")


def _fold(text: str) -> str:
    """Lowercase and strip accents so 'dieciséis' matches 'dieciseis'."""
    decomposed = unicodedata.normalize("NFD", (text or "").lower())
    return "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")


def _value(tokens: list[str]) -> int:
    total, current = 0, 0
    for token in tokens:
        if token in _SCALES:
            scale = _SCALES[token]
            if scale == 1000:
                total += (current or 1) * 1000
            else:
                total = (total + (current or 1)) * scale
            current = 0
        else:
            current += _UNITS[token]
    return total + current


def words_to_digits(text: str) -> str:
    """Every number word run, in order, as its digits (digit-by-digit stays digit-by-digit)."""
    tokens = _TOKEN.findall(_fold(text))
    out: list[str] = []
    run: list[str] = []

    def flush() -> None:
        if not run:
            return
        value = _value(run)
        # "cinco uno cero" arrives as five single digits in a row: keep them adjacent.
        out.append(str(value))
        run.clear()

    for token in tokens:
        if token.isdigit():
            flush()
            out.append(token)
        elif token in _UNITS or token in _SCALES:
            # Digit by digit ("cinco uno cero") is a list of numbers, not a sum: a bare
            # single-digit word after another single-digit word starts a new number.
            if run and _UNITS.get(token, 100) < 10 and all(_UNITS.get(t, 100) < 10 for t in run):
                flush()
            run.append(token)
        elif token in _GLUE and run:
            continue
        else:
            flush()
    flush()
    stream = "".join(out)
    return stream


def candidates(text: str, length: int = 6) -> list[str]:
    """Every `length`-digit window worth testing against a code."""
    literal = "".join(_DIGITS.findall(text or ""))
    spoken = words_to_digits(text or "")
    found: list[str] = []
    for stream in (literal, spoken):
        for start in range(0, max(0, len(stream) - length + 1)):
            found.append(stream[start:start + length])
    return found
