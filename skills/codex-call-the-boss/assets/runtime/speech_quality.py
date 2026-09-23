"""Conservative text alignment for synthetic-output QA, never caller ASR.

Chinese homophones such as 会话/绘画 have identical pronunciation. An exact
character score alone should not misclassify those as lost or corrupt audio.
Keep the original score, tones, syllable counts and raw ASR for inspection.
"""
from __future__ import annotations
import json
import re
import subprocess
from difflib import SequenceMatcher
from pathlib import Path

ALIGNMENT_REVISION = 'orthography-status-opening-v2'

STATE_PHRASES = ('已开始', '已经开始', '已完成', '已经完成', '已经修复', '已修复',
                 '成功', '通过', '送达', '确认', '开始', '完成', '修复')


def critical_content(text: str) -> tuple:
    """Never let a high overall similarity conceal a reversed result/number.

    Negation and numbers are character-strict. Affirmative status labels
    may only be reconciled by a complete, tone-identical phonetic phrase.
    """
    compact = re.sub(r'[^\u3400-\u9fffA-Za-z0-9]', '', text).casefold()
    negative = re.findall(r'没有|没|不能|不要|不得|不会|未|无|不|失败|错误|仍然|尚未', compact)
    numbers = re.findall(r'\d+(?:\.\d+)?', text)
    states = re.findall('|'.join(STATE_PHRASES), compact)
    return tuple(negative), tuple(numbers), tuple(states)


def _phonetic_states(tokens, phrases):
    """Match complete affirmative phrases, including tones and their order.

    This is only output-QA: "以修复" and "已修复" have the same sound. It
    never normalizes a caller's command or relaxes negative/number checks.
    """
    ordered = sorted(enumerate(phrases), key=lambda pair: len(pair[1]), reverse=True)
    matches, offset = [], 0
    while offset < len(tokens):
        for identity, phrase in ordered:
            if phrase and tokens[offset:offset+len(phrase)] == phrase:
                matches.append(identity)
                offset += len(phrase)
                break
        else:
            offset += 1
    return tuple(matches)


def _simplified_pair(expected, actual):
    """Normalize a mismatched output-QA pair with the existing Apple helper.

    A conversion is not a content decision. Keep raw scores/transcripts and
    run every critical-content and alignment check again on the result.
    """
    probe = subprocess.run([
        '/usr/bin/swift', str(Path(__file__).with_name('speech_pronunciation.swift')), '--simplified'],
        input=json.dumps([expected, actual], ensure_ascii=False), text=True,
        capture_output=True, timeout=2, check=True)
    converted = json.loads(probe.stdout)
    if (not isinstance(converted, list) or len(converted) != 2
            or any(not isinstance(value, str) or not value for value in converted)):
        raise ValueError('Invalid orthography result')
    for original, value in zip((expected, actual), converted):
        # Traditional/Simplified conversion does not add/remove characters,
        # alter numbers, or rewrite non-CJK text. Malformed helpers fail closed.
        if (len(original) != len(value)
                or re.sub(r'[\u3400-\u9fff]', '', original) != re.sub(r'[\u3400-\u9fff]', '', value)):
            raise ValueError('Orthography conversion changed non-script content')
    return converted


def speech_alignment(expected: str, actual: str) -> dict:
    normalized = lambda s: re.sub(r'[^\u3400-\u9fffA-Za-z0-9]', '', s).casefold()
    left, right = normalized(expected), normalized(actual)
    protect_opening = left.startswith(('老板', '老闆'))
    same_opening = not protect_opening or right.startswith(left[:2])
    score = SequenceMatcher(None, left, right).ratio() if left and right else 0.0
    result = {'character_similarity': round(score, 4), 'passed': score >= .88,
              'method': 'characters', 'pronunciation_similarity': None}
    if protect_opening:
        result['opening_address_passed'] = same_opening
    expected_critical, actual_critical = critical_content(expected), critical_content(actual)
    # Neither script conversion nor phonetic similarity can change numbers.
    # Clearly changed simplified negation also fails without another process.
    if (expected_critical[1] != actual_critical[1]
            or expected_critical[0] != actual_critical[0]
            and not re.search(r'[沒無會敗錯誤]', expected + actual)):
        result.update(passed=False, method='critical_content_mismatch')
        return result
    if not result['passed'] or expected_critical != actual_critical or not same_opening:
        try:
            canonical_expected, canonical_actual = _simplified_pair(expected, actual)
            if (canonical_expected, canonical_actual) != (expected, actual):
                expected, actual = canonical_expected, canonical_actual
                left, right = normalized(expected), normalized(actual)
                score = SequenceMatcher(None, left, right).ratio() if left and right else 0.0
                result.update(orthography_normalization='traditional_to_simplified',
                              comparison_character_similarity=round(score, 4), passed=score >= .88)
                expected_critical, actual_critical = critical_content(expected), critical_content(actual)
        except (OSError, subprocess.SubprocessError, ValueError, TypeError):
            result['orthography_check_unavailable'] = True
    same_states = expected_critical[2] == actual_critical[2]
    same_opening = not protect_opening or right.startswith(left[:2])
    if protect_opening:
        result['opening_address_passed'] = same_opening
    # Negation and numbers remain character-strict, even for homophones.
    # A changed affirmative label can only be checked by exact phonetics,
    # never by the high overall character score alone.
    if expected_critical[:2] != actual_critical[:2]:
        result.update(passed=False, method='critical_content_mismatch')
        return result
    if not same_states:
        result.update(passed=False, method='critical_content_mismatch')
    if not same_opening:
        # Two incorrect/missing opening syllables can still score >0.95 in a
        # long report. Require the address at the beginning, independently
        # of the global score. Exact homophones need the existing full,
        # tone-preserving comparison below; helper failure stays rejected.
        result.update(passed=False, method='opening_address_mismatch')
    if result['passed'] or score < .75:
        return result
    try:
        probe = subprocess.run(['/usr/bin/swift', str(Path(__file__).with_name('speech_pronunciation.swift'))],
            input=json.dumps([expected, actual, *STATE_PHRASES], ensure_ascii=False), text=True,
            capture_output=True, timeout=10, check=True)
        pronunciations = json.loads(probe.stdout)
        if (not isinstance(pronunciations, list) or len(pronunciations) != 2+len(STATE_PHRASES)
                or any(not isinstance(row, list) or not row
                       or any(not isinstance(token, str) or not token for token in row)
                       for row in pronunciations)):
            raise ValueError('Invalid pronunciation result')
        a, b, *phrases = pronunciations
        phonetic = SequenceMatcher(None, a, b, autojunk=False).ratio() if a and b else 0.0
        ratio = min(len(a), len(b)) / max(len(a), len(b)) if a and b else 0.0
        expected_states = tuple(STATE_PHRASES.index(state) for state in expected_critical[2])
        same_phonetic_states = (_phonetic_states(a, phrases) == expected_states
                                and _phonetic_states(b, phrases) == expected_states)
        same_phonetic_opening = (same_opening or
            (len(a) >= 2 and len(b) >= 2 and a[:2] == b[:2]))
        result.update(pronunciation_similarity=round(phonetic,4), syllable_length_ratio=round(ratio,4),
                      method='characters+tone_preserving_syllables',
                      passed=phonetic >= .94 and ratio >= .94 and same_phonetic_states
                             and same_phonetic_opening)
        if protect_opening and not same_opening:
            result.update(opening_address_phonetically_equal=same_phonetic_opening,
                          opening_address_passed=same_phonetic_opening)
            if not same_phonetic_opening:
                result['method'] = 'opening_address_mismatch'
        if not same_states:
            result['critical_states_phonetically_equal'] = same_phonetic_states
            if not same_phonetic_states:
                result['method'] = 'critical_content_mismatch'
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        result['pronunciation_check_unavailable'] = True
    return result
