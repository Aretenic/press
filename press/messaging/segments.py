"""How many texts a message counts as: up to 140 bytes to one number (Aretenic ADR 094 §8).

160 characters when every character is in the GSM 7-bit alphabet, 70 otherwise; a longer message
is split into parts of 153 or 67. The same count as aretenic's compose box, so the school sees
what it is billed. Twilio's own segment count, returned after sending, is what is recorded.
"""

from __future__ import annotations

# cspell:disable
_GSM_BASIC = set(
	"@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
	"¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
# cspell:enable
_GSM_EXTENDED = set("^{}\\[~]|€\f")


def count(text: str) -> int:
	if not text:
		return 1
	if all(ch in _GSM_BASIC or ch in _GSM_EXTENDED for ch in text):
		length = sum(2 if ch in _GSM_EXTENDED else 1 for ch in text)
		single, part = 160, 153
	else:
		length = sum(2 if ord(ch) > 0xFFFF else 1 for ch in text)
		single, part = 70, 67
	return 1 if length <= single else -(-length // part)
