from __future__ import annotations
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from asr_hotword_guard import is_hotword_echo, require_no_hotword_echoes
from qwen_local_asr import parse_local_result
from qwen_local_worker import run_request

TERMS = ['灰泽满Hazel', '灰泽满', 'Hazel', '芙娅之魂', '回火测试', '终焉地']
ECHO = '灰泽满Hazel、灰泽满、Hazel、芙娅之魂、回火测试、终焉地。'


class HotwordEchoTests(unittest.TestCase):
    def test_detects_real_echo_and_punctuation_variants(self):
        for text in [ECHO, ECHO.replace('、', '，'), ECHO.replace('、', ''), '灰泽满Hazel、灰泽满、Hazel、', '芙娅之魂、回火测试、终焉地。']:
            with self.subTest(text=text):
                self.assertTrue(is_hotword_echo(text, TERMS))

    def test_preserves_spoken_names_and_real_sentences(self):
        for text in ['灰泽满。', 'Hazel', '今天灰泽满在玩芙娅之魂的回火测试，打不过终焉地。',
                     '我第一次玩芙娅之魂。', '热词列表应该怎么设置？']:
            with self.subTest(text=text):
                self.assertFalse(is_hotword_echo(text, TERMS))

    def test_does_not_count_overlapping_aliases_as_several_terms(self):
        self.assertFalse(is_hotword_echo('灰泽满Hazel', TERMS))

    def test_parser_rejects_echo_before_normalization(self):
        payload = {'guard_terms': TERMS, 'result': {'sentence_info': [
            {'start':2100, 'end':2970, 'spk':0, 'sentence':ECHO}]}}
        with self.assertRaisesRegex(ValueError, '热词列表复读'):
            parse_local_result(payload, normalize=lambda _: '嗯。')

    def test_parser_collapses_concatenated_language_labels(self):
        payload = {'result': {'language':'Chinese'*1127, 'sentence_info': [
            {'start':2100, 'end':2970, 'spk':0, 'sentence':'嗯。'}]}}
        rows, _ = parse_local_result(payload)
        self.assertEqual(rows[0]['language'], 'Chinese')

    def test_worker_never_feeds_vocabulary_to_model(self):
        calls = []
        class Model:
            def generate(self, **kwargs):
                calls.append(kwargs)
                return [{'language':'Chinese', 'sentence_info':[
                    {'start':0,'end':800,'sentence':'嗯。'}]}]
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'source.wav'; source.write_bytes(b'test')
            result=run_request({'input':str(source), 'cache_root':str(Path(tmp)/'models'),
                                'model':'local:qwen3-asr-0.6b', 'context':'、'.join(TERMS)},
                               model_factory=lambda **_:Model())
        self.assertEqual(calls[0]['context'], '')
        self.assertEqual(result['guard_terms'], TERMS)
        self.assertEqual(result['context_policy'], 'audio-only-no-hotword-prompt-v2')

    def test_flow1_failed_retry_invalidates_old_pass(self):
        import qwen_local_flow as flow
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); source=root/'source.wav'; source.write_bytes(b'test')
            output=root/'flow1'; output.mkdir()
            gate=output/'transcript-completeness.json'
            gate.write_text(json.dumps({'status':'PASS','authoritative':True}))
            args=SimpleNamespace(input=source,output=output,model='local:qwen3-asr-auto',
                                 initial_prompt='、'.join(TERMS))
            with patch.object(flow,'media_duration',return_value=5), patch.object(
                flow,'transcribe_local_media',side_effect=ValueError('热词列表复读')):
                with self.assertRaisesRegex(ValueError,'热词列表复读'):
                    flow.run_qwen_local_flow1(args,multilingual=False)
            report=json.loads(gate.read_text(encoding="utf-8"))
            self.assertEqual(report['status'],'FAIL')
            self.assertFalse(report['authoritative'])

    def test_old_cache_cannot_generate_subtitles_despite_old_pass(self):
        from content_slicer import load_authoritative_segments
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            transcript=root/'transcript.json'; transcript.write_text('{"segments":[]}')
            csv=root/'transcript.csv'
            csv.write_text('start_seconds,end_seconds,text\n2.1,2.97,'+ECHO+'\n',encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'热词列表复读'):
                load_authoritative_segments(transcript,csv)

    def test_old_cache_cannot_become_selection_context(self):
        import workflow_app_core as core
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); csv=root/'transcript.csv'
            csv.write_text('start_seconds,end_seconds,text\n2.1,2.97,'+ECHO+'\n',encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'热词列表复读'):
                core.build_compact_selection_context(csv,root,root/'context.csv',mode='narrative')


if __name__ == '__main__':
    unittest.main()

