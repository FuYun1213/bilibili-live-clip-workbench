from pathlib import Path
import tempfile
import unittest

import review_speaker_roles as roles
import review_workspace as review

ASS = """[Script Info]
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Main,Arial,50,&H00FFFFFF,&H00FFFFFF,&H00111111,&H00000000,0,0,0,0,100,100,0,0,1,3,2,2,10,10,10,1
Style: Guest,Arial,50,&H00FFFFFF,&H00FFFFFF,&H00111111,&H00000000,0,0,0,0,100,100,0,0,1,3,2,2,10,10,10,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:01.00,0:00:02.00,Main,SPEAKER_00,0,0,0,,第一句,逗号
Dialogue: 0,0:00:03.00,0:00:04.00,Main,SPEAKER_01,0,0,0,,第二句
Dialogue: 0,0:00:05.00,0:00:06.00,Guest,SPEAKER_00,0,0,0,review-context,第三句
Dialogue: 0,0:00:07.00,0:00:08.00,Main,,0,0,0,,未标记甲
Dialogue: 0,0:00:09.00,0:00:10.00,Guest,,0,0,0,,未标记乙
"""
PROFILES = {
    "a": {"display_name": "角色甲", "role_color": "#FFAABB"},
    "b": {"display_name": "角色乙", "role_color": "#33CC99"},
}


class SpeakerRoleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ass = Path(self.temp.name) / 'clip.ass'
        self.ass.write_text(ASS, encoding='utf-8-sig')

    def test_group_assignment_preserves_times_text_effects_and_survives_reopen(self):
        before = review.read_dialogues(self.ass)
        count = roles.apply_speaker_roles(self.ass, {('SPEAKER_00', ''): 'a', ('SPEAKER_01', ''): 'b'}, PROFILES)
        after = review.read_dialogues(self.ass)
        self.assertEqual(count, 3)
        self.assertEqual([row['name'] for row in after], ['角色甲', '角色乙', '角色甲', '', ''])
        self.assertEqual(after[0]['style'], 'Speaker_a')
        self.assertEqual(after[2]['style'], 'Speaker_a')
        for old, new in zip(before, after):
            self.assertEqual(tuple(old[key] for key in ('start', 'end', 'text', 'effect')),
                             tuple(new[key] for key in ('start', 'end', 'text', 'effect')))
        saved = self.ass.read_text(encoding='utf-8-sig')
        self.assertIn('&H00BBAAFF', saved)
        self.assertIn('&H0099CC33', saved)
        self.assertEqual(roles.speaker_groups(after)[0]['count'], 2)

    def test_swapping_speakers_does_not_cascade(self):
        profiles = {'a': {**PROFILES['a'], 'display_name': 'SPEAKER_01'},
                    'b': {**PROFILES['b'], 'display_name': 'SPEAKER_00'}}
        roles.apply_speaker_roles(self.ass, {('SPEAKER_00', ''): 'a', ('SPEAKER_01', ''): 'b'}, profiles)
        self.assertEqual([row['name'] for row in review.read_dialogues(self.ass)][:3],
                         ['SPEAKER_01', 'SPEAKER_00', 'SPEAKER_01'])

    def test_unnamed_speakers_remain_separate_by_style(self):
        roles.apply_speaker_roles(self.ass, {('', 'Guest'): 'b'}, PROFILES)
        rows = review.read_dialogues(self.ass)
        self.assertEqual(rows[3]['name'], '')
        self.assertEqual(rows[4]['name'], '角色乙')

    def test_invalid_profile_or_style_never_partially_changes_ass(self):
        before = self.ass.read_bytes()
        for assignments, profiles in [
            ({('SPEAKER_00', ''): 'missing'}, PROFILES),
            ({('SPEAKER_00', ''): 'a', ('SPEAKER_01', ''): 'b'},
             {**PROFILES, 'b': {'role_color': 'invalid'}}),
        ]:
            with self.subTest(assignments=assignments), self.assertRaises(ValueError):
                roles.apply_speaker_roles(self.ass, assignments, profiles)
            self.assertEqual(self.ass.read_bytes(), before)

if __name__ == '__main__':
    unittest.main()
