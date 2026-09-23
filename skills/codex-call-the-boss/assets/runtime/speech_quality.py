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
# Additive exact-equivalence proof; existing accepted cache proofs remain valid.
NUMERIC_ORTHOGRAPHY_REVISION = 'exact-cardinal-decimal-tone-v2'

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


def _numeric_orthography(text: str) -> str | None:
    """Render small unsigned Arabic cardinals as Chinese for exact QA only.

    No fuzzy number parsing, rounding, unit conversion or caller-text rewrite.
    Leading zero IDs, grouped numbers, versions and large numbers are outside
    this proof. Keep signs, decimal zeros and percent symbols significant.
    """
    if re.search(r'\d[,，]\d|\d+\.\d+\.', text):
        return None
    digits = '零一二三四五六七八九'

    def spell(match):
        literal = match.group()
        integer, dot, fraction = literal.partition('.')
        if len(integer) > 4 or len(integer) > 1 and integer[0] == '0':
            return literal
        value = int(integer)
        if value == 0:
            result = '零'
        else:
            result, pending_zero = '', False
            for power, unit in ((3, '千'), (2, '百'), (1, '十'), (0, '')):
                digit = value // (10 ** power) % 10
                if digit:
                    if pending_zero:
                        result += '零'
                    result += digits[digit] + unit
                    pending_zero = False
                elif result:
                    pending_zero = True
            if result.startswith('一十'):
                result = result[1:]
        return result + ('点' + ''.join(digits[int(c)] for c in fraction) if dot else '')

    expanded = re.sub(r'(?<![A-Za-z0-9_.])[0-9]+(?:\.[0-9]+)?(?![A-Za-z0-9_.])',
                      spell, text)
    # Strip only ordinary speech punctuation, not arbitrary mathematical,
    # currency or full-width signs that might change a number's meaning.
    return re.sub(r'''[\s,，。;；:：、!！?？“”"'‘’]''', '', expanded).casefold()


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


def _numeric_tone_equivalence(left: str, right: str) -> bool:
    """Combine number spelling with full tone identity, not fuzzy similarity.

    Preserve the position of every numeral, sign and non-Chinese character.
    Equal length also forbids a missing/extra syllable from passing this path.
    Negation, states and the opening are checked by the caller before this.
    """
    protected = lambda text: tuple((i, c) for i, c in enumerate(text)
        if not '\u3400' <= c <= '\u9fff' or c in '零〇一二两三四五六七八九十百千万亿点负正')
    if len(left) != len(right) or protected(left) != protected(right):
        return False
    probe = subprocess.run([
        '/usr/bin/swift', str(Path(__file__).with_name('speech_pronunciation.swift'))],
        input=json.dumps([left, right], ensure_ascii=False), text=True,
        capture_output=True, timeout=2, check=True)
    rows = json.loads(probe.stdout)
    if (not isinstance(rows, list) or len(rows) != 2
            or any(not isinstance(row, list) or not row
                   or any(not isinstance(token, str) or not token for token in row)
                   for row in rows)):
        raise ValueError('Invalid numeric pronunciation result')
    return rows[0] == rows[1]


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
    if (expected_critical[1] != actual_critical[1]
            and expected_critical[0] == actual_critical[0]
            and expected_critical[2] == actual_critical[2] and same_opening):
        numeric_left, numeric_right = _numeric_orthography(expected), _numeric_orthography(actual)
        # Only a complete exact match can reconcile number spellings. It cannot
        # lower character/phonetic gates for another word, clause or value.
        if numeric_left and numeric_left == numeric_right:
            result.update(passed=True, method='numeric_orthography_exact',
                          numeric_orthography_revision=NUMERIC_ORTHOGRAPHY_REVISION)
            return result
        if numeric_left and numeric_right:
            try:
                if _numeric_tone_equivalence(numeric_left, numeric_right):
                    result.update(passed=True, method='numeric_orthography+exact_tone_syllables',
                                  pronunciation_similarity=1.0, syllable_length_ratio=1.0,
                                  numeric_orthography_revision=NUMERIC_ORTHOGRAPHY_REVISION)
                    return result
            except (OSError, subprocess.SubprocessError, ValueError, TypeError):
                result['numeric_pronunciation_check_unavailable'] = True
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
