import unittest
from unittest.mock import patch
from types import SimpleNamespace

import speech_quality
from speech_quality import speech_alignment


EXPECTED = ('两处启动问题已经修复并更新到实际服务,音色没改。九百二十八项测试都通过了;'
            '最新无拨号检查里,预热七点三秒、整体准备十六点三秒,也都通过了。')
OBSERVED = ('两处启动问题已经修复并更新到实际服务音色没改928项测试都通过了'
            '最新无拨号检查里预热7.3秒整体准备16.3秒也都通过了')
MIXED_EXPECTED = ('这处误判已经修好,也更新到实际服务里了。刚才原声复验通过,'
                  '九百三十三项测试也都过了,音色和固定回执都没改。')
MIXED_OBSERVED = ('这处误判已经修好也更新到实际服务里了刚才原生复验通过'
                  '933项测试也都过了音色和固定回值都没改')


class NumericOrthographyTests(unittest.TestCase):
    def test_actual_mixed_number_and_homophones_require_full_tone_identity(self):
        result = speech_alignment(MIXED_EXPECTED, MIXED_OBSERVED)
        self.assertTrue(result['passed'])
        self.assertEqual(result['method'], 'numeric_orthography+exact_tone_syllables')
        self.assertEqual(result['character_similarity'], .8723)
        self.assertEqual(result['pronunciation_similarity'], 1.0)

    def test_mixed_changed_tone_is_not_fuzzy_accepted(self):
        for actual in (MIXED_OBSERVED.replace('原生', '原省'),
                       MIXED_OBSERVED.replace('回值', '回纸')):
            with self.subTest(actual=actual):
                self.assertFalse(speech_alignment(MIXED_EXPECTED, actual)['passed'])

    def test_mixed_protected_content_and_missing_words_never_reach_helper(self):
        for actual in (MIXED_OBSERVED.replace('933', '934'),
                       MIXED_OBSERVED.replace('933', '-933'),
                       MIXED_OBSERVED.replace('933', '933%'),
                       MIXED_OBSERVED.replace('没改', '已改'),
                       MIXED_OBSERVED.replace('通过', '完成'),
                       MIXED_OBSERVED.replace('也更新', '更新'),
                       MIXED_OBSERVED.replace('也更新到实际服务里了', '')):
            with self.subTest(actual=actual), patch.object(speech_quality.subprocess, 'run') as helper:
                self.assertFalse(speech_alignment(MIXED_EXPECTED, actual)['passed'])
                helper.assert_not_called()
        with patch.object(speech_quality.subprocess, 'run') as helper:
            self.assertFalse(speech_quality._numeric_tone_equivalence('负-七点三到八点三', '负七点三到-八点三'))
            helper.assert_not_called()

    def test_mixed_unavailable_or_malformed_helper_fails_closed(self):
        with patch.object(speech_quality.subprocess, 'run', side_effect=OSError('unavailable')):
            self.assertFalse(speech_alignment(MIXED_EXPECTED, MIXED_OBSERVED)['passed'])
        for output in ('null', '[]', '[[], []]', '[[1],[1]]', 'not json', '[["a"],["b"]]'):
            with self.subTest(output=output), patch.object(speech_quality.subprocess, 'run',
                    return_value=SimpleNamespace(stdout=output)):
                self.assertFalse(speech_alignment(MIXED_EXPECTED, MIXED_OBSERVED)['passed'])

    def test_actual_rejected_answer_is_exact_equivalence_without_helper(self):
        with patch.object(speech_quality.subprocess, 'run') as helper:
            result = speech_alignment(EXPECTED, OBSERVED)
        self.assertTrue(result['passed'])
        self.assertEqual(result['method'], 'numeric_orthography_exact')
        self.assertEqual(result['character_similarity'], .8333)
        helper.assert_not_called()

    def test_values_signs_precision_order_negation_and_clauses_stay_strict(self):
        variants = (
            OBSERVED.replace('928', '929'), OBSERVED.replace('7.3', '7.03'),
            OBSERVED.replace('7.3', '-7.3'), OBSERVED.replace('7.3', '+7.3'),
            OBSERVED.replace('7.3', '－7.3'), OBSERVED.replace('7.3', '＋7.3'),
            OBSERVED.replace('7.3', '7.3%'), OBSERVED.replace('7.3', '(7.3)'),
            OBSERVED.replace('7.3', '7.3×'), OBSERVED.replace('7.3', '$7.3'),
            OBSERVED.replace('7.3', '7.30'), OBSERVED.replace('7.3', '73'),
            OBSERVED.replace('7.3', '16.3').replace('准备16.3', '准备7.3'),
            OBSERVED.replace('音色没改', '音色改了'),
            OBSERVED.replace('已经修复', '已经完成'),
            OBSERVED.replace('也都通过了', ''),
            OBSERVED.replace('928', '0928'), OBSERVED.replace('928', '9.28'),
        )
        with patch.object(speech_quality.subprocess, 'run', side_effect=OSError('unavailable')):
            for actual in variants:
                with self.subTest(actual=actual):
                    self.assertFalse(speech_alignment(EXPECTED, actual)['passed'])

    def test_cardinals_decimals_and_reverse_direction(self):
        for words, number in (('零', '0'), ('十', '10'), ('十一', '11'), ('二十', '20'),
                ('一百零一', '101'), ('一千零一', '1001'), ('一千零一十', '1010'),
                ('九千九百九十九', '9999'), ('零点零三', '0.03'), ('七点三零', '7.30')):
            for expected, actual in ((words, number), (number, words)):
                with self.subTest(expected=expected, actual=actual):
                    self.assertTrue(speech_alignment('耗时' + expected + '秒', '耗时' + actual + '秒')['passed'])

    def test_opening_address_cannot_use_number_equivalence_to_bypass_gate(self):
        for prefix in ('老爸', '老版', '', '嗯老板'):
            self.assertFalse(speech_alignment('老板，耗时七点三秒', prefix + '耗时7.3秒')['passed'])

    def test_unsupported_or_ambiguous_formats_do_not_receive_exact_proof(self):
        for expected, actual in (('版本一二三', '版本1.2.3'), ('编号零一', '编号01'),
                ('耗时一万秒', '耗时10000秒'), ('耗时一千秒', '耗时1,000秒'),
                ('有一点问题', '有1个问题'), ('占七点三', '占7.3%'),
                ('版本v一', '版本v1'), ('耗时七点三秒', '耗时7.3.0秒')):
            with self.subTest(expected=expected, actual=actual):
                self.assertFalse(speech_alignment(expected, actual)['passed'])


if __name__ == '__main__':
    unittest.main()
