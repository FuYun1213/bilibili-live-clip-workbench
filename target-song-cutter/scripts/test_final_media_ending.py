import unittest
from audit_final_delivery import av_integrity_issues

class AudioVideoEndingTests(unittest.TestCase):
    def media(self, video, audio, start=0):
        return {'format':{'duration':str(max(video,audio))},'streams':[{'codec_type':'video','duration':str(video),'start_time':str(start)},{'codec_type':'audio','duration':str(audio),'start_time':'0'}]}
    def test_container_duration_does_not_hide_missing_five_second_picture_tail(self):
        self.assertTrue(av_integrity_issues(self.media(25.08,30.21)))
    def test_normal_codec_padding_passes(self):
        self.assertEqual(av_integrity_issues(self.media(30.20,30.22)),[])
    def test_stream_start_offset_is_part_of_ending(self):
        self.assertEqual(av_integrity_issues(self.media(29.0,30.0,start=1.0)),[])
    def test_missing_and_unknown_audio_are_not_reported_as_success(self):
        self.assertTrue(av_integrity_issues({'streams':[{'codec_type':'video','duration':'30'}]}))
        self.assertTrue(av_integrity_issues({'streams':[{'codec_type':'video','duration':'30'},{'codec_type':'audio','duration':'nan'}]}))
