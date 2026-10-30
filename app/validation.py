"""Validação e formatação de telefone/WhatsApp e CPF.

Regras (a pedido do usuário):
  * Telefone/WhatsApp: DDD + número, no máximo 11 dígitos (DDD + 9 dígitos).
    Aceita também 10 dígitos (DDD + 8, telefone fixo). Letras são recusadas.
    Formato gravado: "(92) 9 9815-8767" (celular) ou "(92) 3234-5678" (fixo).
  * CPF: exatamente 11 dígitos, gravado como "999.888.777-54". Letras são recusadas.
    Os dígitos verificadores NÃO são conferidos: só o formato é imposto.

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
    return f"{digits[:3]}.{digits[3:6]}.{digits[6:9]}-{digits[9:]}"
