"""Validação e formatação de telefone/WhatsApp e CPF.

Regras (a pedido do usuário):
  * Telefone/WhatsApp: DDD + número, no máximo 11 dígitos (DDD + 9 dígitos).
    Aceita também 10 dígitos (DDD + 8, telefone fixo). Letras são recusadas.
    Formato gravado: "(92) 9 9815-8767" (celular) ou "(92) 3234-5678" (fixo).
  * CPF: exatamente 11 dígitos, gravado como "999.888.777-54". Letras são recusadas.
    Os dígitos verificadores são conferidos (CPFs com todos os dígitos iguais são recusados).

Os validadores devem ser usados apenas em esquemas de ENTRADA. Em esquemas de resposta eles
quebrariam a listagem inteira se um registro antigo tiver valor fora do padrão.
"""

from __future__ import annotations

import re

PHONE_HELP = "Informe o DDD e o número, somente com números (ex.: (92) 9 9815-8767)."
CPF_HELP = "O CPF deve ter 11 números (ex.: 999.888.777-54)."


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value)


def _has_letters(value: str) -> bool:
    return any(ch.isalpha() for ch in value)


def normalize_phone(value: str | None, label: str = "Telefone") -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if _has_letters(text):
        raise ValueError(f"{label} inválido: não use letras. {PHONE_HELP}")
    digits = _digits(text)
    if len(digits) not in (10, 11):
        raise ValueError(f"{label} inválido: {len(digits)} dígitos informados. {PHONE_HELP}")
    if digits[0] == "0" or digits[1] == "0":
        raise ValueError(f"{label} inválido: DDD incorreto. {PHONE_HELP}")
    if len(digits) == 11:
        if digits[2] != "9":
            raise ValueError(f"{label} inválido: o celular com 9 dígitos começa com 9 depois do DDD. {PHONE_HELP}")
        return f"({digits[:2]}) {digits[2]} {digits[3:7]}-{digits[7:]}"
    return f"({digits[:2]}) {digits[2:6]}-{digits[6:]}"


def normalize_cpf(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if _has_letters(text):
        raise ValueError(f"CPF inválido: não use letras. {CPF_HELP}")
    digits = _digits(text)
    if len(digits) != 11:
        raise ValueError(f"CPF inválido: {len(digits)} dígitos informados. {CPF_HELP}")
    if not _cpf_check_digits_ok(digits):
        raise ValueError("CPF inválido: os dígitos verificadores não conferem.")
    return f"{digits[:3]}.{digits[3:6]}.{digits[6:9]}-{digits[9:]}"


def _cpf_check_digits_ok(digits: str) -> bool:
    if len(set(digits)) == 1:
        return False
    for size in (9, 10):
        total = sum(int(digits[i]) * (size + 1 - i) for i in range(size))
        if (total * 10 % 11) % 10 != int(digits[size]):
            return False
    return True


SERIAL_MESSAGE = "Número de série incorreto. O número de série deve conter exatamente 15 dígitos."


def normalize_serial(value: str | None) -> str:
    """Número de série: exatamente 15 dígitos, guardado como TEXTO (zeros à esquerda preservados)."""
    text = "" if value is None else str(value).strip()
    if not text.isascii() or not text.isdigit() or len(text) != 15:
        raise ValueError(SERIAL_MESSAGE)
    return text
