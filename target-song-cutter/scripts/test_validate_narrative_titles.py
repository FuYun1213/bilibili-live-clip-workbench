import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from validate_narrative_titles import errors_for, main


class NarrativeTitleVocabularyTests(unittest.TestCase):
    def test_new_creator_event_actions_are_valid(self) -> None:
        titles = (
            "小粉螈商用稿报价2000却翻车，枝堇退款还被画师打叉【枝堇Sumire】",
            "枝堇现场拆解小妈赛道，快亲上时才想起辈分【枝堇Sumire】",
            "小松绿教观众向领导哈气，没过多久他真被辞退【小松绿Viridis】",
            "陌生女孩突然攥脚强行擦鞋，小松绿因为礼貌没踹【小松绿Viridis】",
        )
        for title in titles:
            with self.subTest(title=title):
                self.assertEqual(errors_for(title), [])

    def test_creator_name_after_cause_clause_is_still_an_explicit_subject(self) -> None:
        titles = (
            "被七夕安抚梗触发后，小松绿提醒观众先处理现实生活",
            "上班话题升级时，小松绿宣布改成全天轻聊而非工作模式",
            "小松绿“公司倒闭”梗后，她反而说公司没倒闭于是坚持继续播",
            "擦鞋拐住经历触发转折，小松绿讲明因对方女生而克制不出手",
            "推销追问升级到拉扯出钱，小松绿转而给出先拒绝再离开的结论",
        )
        for title in titles:
            with self.subTest(title=title):
                self.assertEqual(errors_for(title), [])
    def test_subjectless_viewpoint_title_still_fails(self) -> None:
        errors = errors_for("只看一个主播才是地狱，一天能看16小时")
        self.assertIn("title body lacks an explicit named subject", errors)

    def test_natural_chinese_outcomes_from_real_codex_output_are_valid(self) -> None:
        titles = (
            "主播从百件下播争议过渡到穿搭底线的自我解释",
            "主播从小时候难喝到越喝越香，维他口味认知被重写",
            "主播讲清礼物规则后又卡在文件流程的服务纠错",
            "昼夜听见耳洞话题后先心动又顾虑，最终决定用耳夹代替打孔",
            "弹幕挑逗引发大脑失速后，主播转入唱歌并改讲头像平稳收场",
        )
        for title in titles:
            with self.subTest(title=title):
                self.assertEqual(errors_for(title), [])

    def test_user_preferred_hook_titles_do_not_require_formulaic_result_words(self) -> None:
        titles = (
            "米女妖别脑控我了【灰泽满Hazel】",
            "宣战🌿《夏天的风》谁才是V圈第一歌势‼️【灰泽满Hazel】",
            "最速人设崩塌！大大方方看斗罗大陆，不觉得炼器筑基很神圣嘛【枝堇Sumire】",
            "羽啾总结男生玩抽象只有可莉和奶龙痛衣两条路，最后改成七夕送女友指南【羽啾chu2u】",
        )
        for title in titles:
            with self.subTest(title=title):
                self.assertEqual(errors_for(title), [])

    def test_choices_conditions_and_rules_are_concrete_events(self) -> None:
        titles = (
            "我爸的二选一：不交社保就出去上班【羽啾chu2u】",
            "羽啾家的新规定：不按时吃饭就不能继续唱歌【羽啾chu2u】",
            "朋友给出两个选择，要么道歉要么直接拉黑【羽啾chu2u】",
            "准备12首先删4首，刚要开唱又忘了从哪里开始：歌单呢？【小松绿Viridis】",
            "下午四点多早退只是提前一点点！明天这么多人，总不能还是我先走吧【小松绿Viridis】",
        )
        for title in titles:
            with self.subTest(title=title):
                self.assertEqual(errors_for(title), [])

    def test_real_codex_state_choice_and_question_titles_are_events(self) -> None:
        titles = (
            "【哎小呜】太过底边，反而成了底边中的知名人物",
            "【哎小呜】VR和PSP同时掉水里救谁？我得等他们救我，因为我只会悬浮",
            "【小松绿Viridis】户外电台选址：既要偏僻又要有网！下地拔草不算，纯遛弯",
        )
        for title in titles:
            with self.subTest(title=title):
                self.assertEqual(errors_for(title), [])

    def test_topic_nouns_without_an_event_still_fail(self) -> None:
        errors = errors_for("羽啾的社保与上班话题【羽啾chu2u】")
        self.assertIn("title body lacks a payoff or clickable hook", errors)

    def test_warn_only_reports_invalid_title_without_failing_flow(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "titles.csv"
            path.write_text(
                "clip_id,title,content_type\n001,筑基大能,narrative\n",
                encoding="utf-8-sig",
            )
            with patch.object(
                sys, "argv", ["validate_narrative_titles.py", "--warn-only", str(path)]
            ):
                self.assertEqual(main(), 0)

    def test_generic_codex_placeholder_still_fails(self) -> None:
        errors = errors_for("【枝堇Sumire】001 标题为通过完整叙事")
        self.assertIn("title is a generic placeholder", errors)

if __name__ == "__main__":
    unittest.main()
