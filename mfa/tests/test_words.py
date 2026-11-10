"""Number words the ASR produced must reach the same six digits as the code."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import words as W  # noqa: E402


def test_digit_by_digit():
    assert W.words_to_digits("cinco uno cero ocho dos nueve") == "510829"


def test_full_number_words():
    assert W.words_to_digits("quinientos diez mil ochocientos veintinueve") == "510829"


def test_thousands_with_y():
    assert "49345" in W.words_to_digits("cuarenta y nueve mil trescientos cuarenta y cinco")


def test_accents_are_ignored():
    assert W.words_to_digits("dieciséis") == "16"


def test_literal_digits_survive_and_join_words():
    assert W.words_to_digits("el 649.345 de la reserva") == "649345"
    assert W.words_to_digits("cero siete, y luego nueve") == "079"


def test_candidates_offer_both_streams():
    found = W.candidates("digo cinco uno cero ocho dos nueve")
    assert "510829" in found


def test_noise_around_the_number_does_not_break_it():
    text = "hacecitos cuarenta y nueve mil trescientos cuarenta y cinco, Ninona"
    assert "49345" in W.words_to_digits(text)
