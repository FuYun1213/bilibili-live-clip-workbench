import unittest
from types import SimpleNamespace
from unittest.mock import patch

from transcribe_review_clips import (
    ass_cues,
    find_uncovered_speech_windows,
    recover_transcript_gaps,
    tighten_cue_to_speech,
    whisper_segment_record,
)


class SubtitleInternalGapRegressionTests(unittest.TestCase):
    def test_tiny_leading_overlap_does_not_hide_internal_silence(self):
        cue = {"start_seconds": 1.0, "end_seconds": 2.5, "text": "测试"}
        adjusted, _ = tighten_cue_to_speech(
            cue, [(0.8, 1.05), (1.7, 2.3)], min_overlap=0.08
        )
        self.assertIsNotNone(adjusted)
        self.assertAlmostEqual(adjusted["start_seconds"], 1.64)

    def test_latin_subword_fragments_stay_in_one_cue(self):
        segment = {
            "start_seconds": 0.0,
            "end_seconds": 1.0,
            "text": "staff",
            "words": [
                {"start_seconds": 0.0, "end_seconds": 0.4, "word": " st"},
                {"start_seconds": 0.4, "end_seconds": 1.0, "word": "aff"},
            ],
        }
        cues = ass_cues(segment, max_chars=2, max_duration=0.2)
        self.assertEqual([cue["text"] for cue in cues], ["staff"])
    def test_long_chinese_sentence_splits_at_complete_clause_connectors(self):
        sentence = "这句话虽然比较长但是主播还没有把完整意思说完所以不能提前截断。"
        words = [
            {"start_seconds": index * 0.12, "end_seconds": (index + 1) * 0.12, "word": char}
            for index, char in enumerate(sentence)
        ]
        segment = {
            "start_seconds": 0.0,
            "end_seconds": len(sentence) * 0.12,
            "text": sentence,
            "words": words,
        }
        cues = ass_cues(segment)
        self.assertEqual(
            [cue["text"] for cue in cues],
            [
                "这句话虽然比较长但是主播还没有把完整意思说完",
                "所以不能提前截断。",
            ],
        )
        self.assertEqual("".join(cue["text"] for cue in cues), sentence)
        self.assertTrue(all("\\N" not in cue["text"] for cue in cues))
        self.assertTrue(all(len(cue["text"].replace(" ", "")) <= 24 for cue in cues))

    def test_soft_target_does_not_cut_a_complete_clause_at_eighteen_chars(self):
        sentence = "我妈后来发现我每天只需要工作两个小时所以她同意我继续做主播了"
        words = [
            {"start_seconds": index * 0.12, "end_seconds": (index + 1) * 0.12, "word": char}
            for index, char in enumerate(sentence)
        ]
        cues = ass_cues({
            "start_seconds": 0.0,
            "end_seconds": len(sentence) * 0.12,
            "text": sentence,
            "words": words,
        })
        self.assertEqual(
            [cue["text"] for cue in cues],
            ["我妈后来发现我每天只需要工作两个小时", "所以她同意我继续做主播了"],
        )
    def test_moderate_word_timestamp_gap_does_not_split_one_lexical_word(self):
        for text in ("女儿", "说服", "他们", "即日", "突然", "有时候", "超跑"):
            with self.subTest(text=text):
                words = []
                for index, char in enumerate(text):
                    start = index * 0.2 if index == 0 else 1.5 + (index - 1) * 0.2
                    words.append({
                        "start_seconds": start,
                        "end_seconds": start + 0.2,
                        "word": char,
                    })
                cues = ass_cues({
                    "start_seconds": 0.0,
                    "end_seconds": words[-1]["end_seconds"],
                    "text": text,
                    "words": words,
                })
                self.assertEqual([cue["text"] for cue in cues], [text])

    def test_extreme_gap_overrides_lexical_protection(self):
        words = [
            {"start_seconds": 0.0, "end_seconds": 0.2, "word": "好"},
            {"start_seconds": 3.2, "end_seconds": 3.6, "word": "笑"},
        ]
        cues = ass_cues({
            "start_seconds": 0.0,
            "end_seconds": 3.6,
            "text": "好笑",
            "words": words,
        })
        self.assertEqual([cue["text"] for cue in cues], ["好", "笑"])

    def test_vad_positive_asr_empty_region_becomes_recovery_window(self):
        segments = [{
            "start_seconds": 0.0,
            "end_seconds": 8.0,
            "text": "已有宽段",
            "words": [
                {"start_seconds": 1.0, "end_seconds": 1.8, "word": "已有"},
            ],
        }]
        windows = find_uncovered_speech_windows(
            [(1.0, 1.8), (4.0, 5.0)],
            segments,
            duration=8.0,
        )
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0]["core_start"], 4.0)
        self.assertEqual(windows[0]["core_end"], 5.0)
        self.assertEqual(windows[0]["start"], 3.4)
        self.assertEqual(windows[0]["end"], 5.6)

    def test_exact_core_auto_language_recovers_song_missed_by_padded_retry(self):
        class Model:
            def __init__(self):
                self.calls = []

            def transcribe(self, audio, **options):
                self.calls.append((len(audio), dict(options)))
                if len(audio) > 32_000:
                    return iter(()), SimpleNamespace(language="zh")
                item = SimpleNamespace(
                    start=0.0,
                    end=2.0,
                    text=" You didn't have to cut me off",
                    avg_logprob=-0.3,
                    no_speech_prob=0.01,
                    words=[
                        SimpleNamespace(
                            start=0.0,
                            end=0.8,
                            word=" You didn't",
                            probability=0.82,
                        ),
                        SimpleNamespace(
                            start=0.8,
                            end=2.0,
                            word=" have to cut me off",
                            probability=0.94,
                        ),
                    ],
                )
                return iter((item,)), SimpleNamespace(language="en")

        model = Model()
        with patch(
            "faster_whisper.audio.decode_audio",
            return_value=[0.0] * 160_000,
        ):
            recovered, report = recover_transcript_gaps(
                model,
                SimpleNamespace(),
                [],
                [(4.0, 6.0)],
                10.0,
                language=None,
                beam_size=5,
                prompt="中文热词",
                replacements={},
                hallucination_silence_threshold=1.0,
                padding=0.6,
            )

        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]["gap_recovery_pass"], "exact-auto-language")
        self.assertEqual(recovered[0]["start_seconds"], 4.0)
        self.assertEqual(recovered[0]["end_seconds"], 6.0)
        self.assertEqual(report["exact_auto_language_recovered_segment_count"], 1)
        self.assertEqual(report["exact_auto_language_attempts"][0]["detected_language"], "en")
        # Source recovery must remain unbiased in every pass. The legacy
        # prompt argument is not permission to reintroduce hotword injection.
        self.assertGreaterEqual(len(model.calls), 2)
        for _, options in model.calls:
            self.assertNotIn("hotwords", options)
            self.assertNotIn("initial_prompt", options)

    def test_partial_hole_inside_one_vad_region_is_recovered(self):
        segments = [{
            "start_seconds": 1.0,
            "end_seconds": 5.0,
            "text": "只识别了前半段",
            "words": [
                {"start_seconds": 1.0, "end_seconds": 2.0, "word": "前半段"},
            ],
        }]
        windows = find_uncovered_speech_windows(
            [(1.0, 5.0)],
            segments,
            duration=6.0,
        )
        self.assertEqual(len(windows), 1)
        self.assertAlmostEqual(windows[0]["core_start"], 2.12)
        self.assertEqual(windows[0]["core_end"], 5.0)

    def test_primary_serialization_keeps_original_text_and_zero_length_words(self):
        item = SimpleNamespace(
            start=1.0,
            end=2.0,
            text="好笑吗",
            avg_logprob=-0.2,
            no_speech_prob=0.01,
            words=[
                SimpleNamespace(start=1.0, end=1.2, word="好", probability=0.9),
                SimpleNamespace(start=1.2, end=1.2, word="笑", probability=0.8),
                SimpleNamespace(start=1.2, end=1.5, word="吗", probability=0.9),
            ],
        )
        record = whisper_segment_record(item, {})
        self.assertEqual(record["text"], "好笑吗")
        self.assertEqual(len(record["words"]), 3)

    def test_real_long_pause_still_separates_complete_utterances(self):
        words = [
            {"start_seconds": index * 0.2, "end_seconds": (index + 1) * 0.2, "word": char}
            for index, char in enumerate("我没驾照")
        ]
        words.extend([
            {"start_seconds": 3.0 + index * 0.2, "end_seconds": 3.2 + index * 0.2, "word": (" " if index == 0 else "") + char}
            for index, char in enumerate("太好了")
        ])
        cues = ass_cues({
            "start_seconds": 0.0,
            "end_seconds": words[-1]["end_seconds"],
            "text": "我没驾照 太好了",
            "words": words,
        })
        self.assertEqual([cue["text"] for cue in cues], ["我没驾照", "太好了"])
    def test_cue_never_ends_on_a_hard_grammatical_connector(self):
        sentence = "结果我一天只要就是有时候上班不多因为我告诉她每天只上两小时班"
        words = [
            {"start_seconds": index * 0.12, "end_seconds": (index + 1) * 0.12, "word": char}
            for index, char in enumerate(sentence)
        ]
        cues = ass_cues({
            "start_seconds": 0.0,
            "end_seconds": len(sentence) * 0.12,
            "text": sentence,
            "words": words,
        })
        self.assertEqual("".join(cue["text"] for cue in cues), sentence)
        self.assertTrue(
            all(not cue["text"].endswith(("只要", "因为")) for cue in cues)
        )
    def test_connector_before_pause_moves_to_the_clause_it_introduces(self):
        first = "如果不行再说吧可是"
        second = "为什么不是那个"
        words = [
            {"start_seconds": index * 0.2, "end_seconds": (index + 1) * 0.2, "word": char}
            for index, char in enumerate(first)
        ]
        offset = words[-1]["end_seconds"] + 2.0
        words.extend([
            {"start_seconds": offset + index * 0.2, "end_seconds": offset + (index + 1) * 0.2, "word": char}
            for index, char in enumerate(second)
        ])
        cues = ass_cues({
            "start_seconds": 0.0,
            "end_seconds": words[-1]["end_seconds"],
            "text": first + second,
            "words": words,
        })
        self.assertTrue(all(not cue["text"].endswith("可是") for cue in cues))
        self.assertTrue(any(cue["text"].startswith("可是为什么") for cue in cues))
    def test_complete_chinese_sentences_split_at_terminal_punctuation(self):
        sentence = "第一句话说完了。第二句话也完整说完了。"
        words = [
            {"start_seconds": index * 0.15, "end_seconds": (index + 1) * 0.15, "word": char}
            for index, char in enumerate(sentence)
        ]
        cues = ass_cues({
            "start_seconds": 0.0,
            "end_seconds": len(sentence) * 0.15,
            "text": sentence,
            "words": words,
        })
        self.assertEqual(
            [cue["text"] for cue in cues],
            ["第一句话说完了。", "第二句话也完整说完了。"],
        )

    def test_normal_wordless_speech_longer_than_24_seconds_keeps_its_start(self):
        text = (
            "经典日系，嗯，点点宝藏的这日推吗？啊，这个歌单里的歌都蛮好听的。"
            "其实就是声，好听到那种会在满下播的时候，如果切到一首喜欢的歌，"
            "会就不舍得拔这个声卡，就会就会等着这首歌放完，然后手玩玩会手机，"
            "然后再再把电脑合上，这种程度。"
        )
        for start in (0.0, 50.0):
            with self.subTest(start=start):
                cues = ass_cues({
                    "start_seconds": start, "end_seconds": start + 25.42,
                    "text": text, "words": [],
                })
                self.assertAlmostEqual(cues[0]["start_seconds"], start)
                self.assertAlmostEqual(cues[-1]["end_seconds"], start + 25.42)
                self.assertEqual("".join(cue["text"] for cue in cues), text)
                self.assertGreater(len(cues), 1)

    def test_sparse_wordless_first_cue_is_clamped_to_spoken_tail(self):
        cues = ass_cues({
            "start_seconds": 0.0,
            "end_seconds": 150.0,
            "text": "怎么这么准时，因为今天提前了一点",
            "words": [],
        })
        self.assertEqual(len(cues), 1)
        self.assertEqual(cues[0]["end_seconds"], 150.0)
        self.assertLessEqual(150.0 - cues[0]["start_seconds"], 12.0)
        self.assertGreater(cues[0]["start_seconds"], 140.0)

    def test_wordless_long_sentence_is_split_proportionally_across_full_timing(self):
        text = "这是一句内容很长但是语速正常而且意思连续完整的中文句子" * 2
        cues = ass_cues({
            "start_seconds": 2.0,
            "end_seconds": 17.0,
            "text": text,
            "words": [],
        })
        self.assertGreater(len(cues), 1)
        self.assertEqual("".join(cue["text"] for cue in cues), text)
        self.assertEqual(cues[0]["start_seconds"], 2.0)
        self.assertEqual(cues[-1]["end_seconds"], 17.0)
        self.assertTrue(all(len(cue["text"].replace(" ", "")) <= 24 for cue in cues))
        self.assertTrue(
            all(cue["end_seconds"] - cue["start_seconds"] <= 7.0 for cue in cues)
        )
    def test_delay_that_moves_fragment_off_speech_rejects_cue(self):
        cue = {"start_seconds": 1.0, "end_seconds": 1.1, "text": "这"}
        adjusted, qa = tighten_cue_to_speech(
            cue, [(1.0, 1.1)], min_overlap=0.08, delay=0.1
        )
        self.assertIsNone(adjusted)
        self.assertEqual(qa["status"], "rejected_after_delay_no_speech")


    def test_delay_does_not_shift_a_normal_speech_start(self):
        cue = {"start_seconds": 1.0, "end_seconds": 2.5, "text": "测试"}
        adjusted, _ = tighten_cue_to_speech(
            cue, [(0.9, 2.3)], min_overlap=0.08, delay=0.1
        )
        self.assertIsNotNone(adjusted)
        self.assertAlmostEqual(adjusted["start_seconds"], 1.0)

    def test_delay_reclamps_past_previous_speech_fragment(self):
        cue = {"start_seconds": 1.0, "end_seconds": 2.5, "text": "测试"}
        adjusted, _ = tighten_cue_to_speech(
            cue,
            [(0.8, 1.092), (1.5, 2.3)],
            min_overlap=0.08,
            delay=0.1,
        )
        self.assertIsNotNone(adjusted)
        self.assertAlmostEqual(adjusted["start_seconds"], 1.44)


    def test_flow1_pause_tolerance_ignores_short_word_gap_but_keeps_long_blank(self):
        short_pause = [{
            "start_seconds": 1.0,
            "end_seconds": 4.0,
            "text": "前后",
            "words": [
                {"start_seconds": 1.0, "end_seconds": 1.5, "word": "前"},
                {"start_seconds": 2.5, "end_seconds": 3.0, "word": "后"},
            ],
        }]
        self.assertEqual(
            find_uncovered_speech_windows(
                [(1.0, 3.0)],
                short_pause,
                duration=4.0,
                min_region=2.0,
                coverage_merge_gap=1.75,
            ),
            [],
        )
        long_blank = [{
            "start_seconds": 1.0,
            "end_seconds": 8.0,
            "text": "前后",
            "words": [
                {"start_seconds": 1.0, "end_seconds": 1.5, "word": "前"},
                {"start_seconds": 6.5, "end_seconds": 7.0, "word": "后"},
            ],
        }]
        windows = find_uncovered_speech_windows(
            [(1.0, 7.0)],
            long_blank,
            duration=8.0,
            min_region=2.0,
            coverage_merge_gap=1.75,
        )
        self.assertEqual(len(windows), 1)
        self.assertGreater(windows[0]["core_end"] - windows[0]["core_start"], 4.0)
if __name__ == "__main__":
    unittest.main()
