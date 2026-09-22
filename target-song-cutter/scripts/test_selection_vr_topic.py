import csv
import tempfile
import unittest
from pathlib import Path
import workflow_app_core as core
import workflow_auto as auto
from validate_selection_tags import validate_tag_row

class SelectionVrTopicTests(unittest.TestCase):
    def payload(self):
        return [{"标题": "昼夜年轻时打算填VR报名表，如今先考虑能否养活自己", "副标题": "年轻时想报名\n先问能否养活", "时间戳": [{"开始秒": 0, "结束秒": 36}], "标签": ["VR报名", "生活成本", "兼职", "收入压力", "职业选择"], "标签依据": "00:01讨论VR报名、生活成本、兼职、收入压力和职业选择", "vr_topic": "yes"}]

    def test_confirmed_topic_survives_import_validation_and_review_csv(self):
        state={"config": {"creator": "chilly", "mode": "narrative"}}
        items=core.normalize_selection(self.payload(), "chilly", "narrative")
        public=core.canonical_selection(items, "narrative")
        self.assertEqual(public[0]["vr_topic"], "yes")
        auto.validate_selection_payload(public, state)
        reloaded=core.normalize_selection(public, "chilly", "narrative")
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/"slices.csv").write_text("slice_id,file\n001,001_test.mp4\n",encoding="utf-8")
            core.write_copy_csv(root/"copy.csv",reloaded,root/"slices.csv","narrative")
            with (root/"copy.csv").open(encoding="utf-8-sig",newline="") as handle:
                row=next(csv.DictReader(handle))
            self.assertEqual(row["vr_topic"], "yes")
            base=core.load_profiles()["chilly"].get("upload",{}).get("tags",[])
            self.assertEqual(validate_tag_row(row,"chilly",base,strict_quality=True),[])

    def test_unconfirmed_vr_tag_still_fails_existing_gate(self):
        for value in (None,"","no",False):
            with self.subTest(value=value):
                payload=self.payload()
                if value is None:payload[0].pop("vr_topic")
                else:payload[0]["vr_topic"]=value
                with self.assertRaisesRegex(auto.SelectionQualityError,"vr_topic=yes"):
                    auto.validate_selection_payload(payload,{"config":{"creator":"chilly","mode":"narrative"}})

    def test_schema_supports_explicit_topic_marker_in_all_modes(self):
        for mode in ("narrative","song","mixed"):
            schema=auto.selection_schema(mode)
            for prop in schema["properties"].values():
                item=prop["items"]
                self.assertIn("vr_topic",item["required"])
                self.assertEqual(item["properties"]["vr_topic"]["enum"],["","yes"])

if __name__ == "__main__":unittest.main()
