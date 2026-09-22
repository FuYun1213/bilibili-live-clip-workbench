import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import biliup_publish as publish


class ReplacementPublishTests(unittest.TestCase):
    def make_context(self, root: Path):
        video = root / "001.mp4"
        video.write_bytes(b"new video")
        cover = root / "cover.jpg"
        cover.write_bytes(b"cover")
        cookie = root / "cookies.json"
        cookie_payload = json.loads(json.dumps(publish.COOKIE_TEMPLATE))
        cookie_payload["cookie_info"]["cookies"][0]["value"] = "sess"
        cookie_payload["cookie_info"]["cookies"][1]["value"] = "csrf"
        cookie.write_text(json.dumps(cookie_payload), encoding="utf-8")
        item = {
            "clip_id": "001", "video": video, "cover": cover,
            "copyright": 1, "source": "", "tid": 27,
            "title": "新版标题", "description": "新版简介",
            "tags": ["切片", "直播"], "no_reprint": 0,
        }
        plan = [{"item": item, "bvid": "BV1234567890", "previous_title": "旧版"}]
        return item, plan, cookie

    def test_replacement_plan_requires_exact_clip_id_mapping(self):
        item = {"clip_id": "001", "title": "新版"}
        plan = publish.replacement_plan(
            [item], {"001": {"title": "旧版", "bvid": "BV1234567890"}}
        )
        self.assertEqual(plan[0]["bvid"], "BV1234567890")
        with self.assertRaisesRegex(ValueError, "clip_id"):
            publish.replacement_plan(
                [{"clip_id": "002", "title": "新版"}],
                {"001": {"title": "旧版", "bvid": "BV1234567890"}},
            )

    def test_append_command_targets_existing_bvid(self):
        item = {
            "video": Path("new.mp4"), "copyright": 1, "source": "",
            "tid": 27, "cover": Path("cover.jpg"), "title": "新版标题",
            "description": "新版简介", "tags": ["切片"], "no_reprint": 0,
        }
        command = publish.build_append_command(
            "biliup", Path("cookies.json"), item, "BV1234567890", 3
        )
        self.assertIn("append", command)
        self.assertNotIn("upload", command)
        self.assertEqual(command[command.index("--vid") + 1], "BV1234567890")

    def test_replacement_edit_payload_keeps_only_new_video(self):
        item = {
            "copyright": 1, "source": "", "tid": 27, "title": "新版标题",
            "description": "新版简介", "tags": ["切片"], "no_reprint": 0,
        }
        data = {
            "archive": {
                "aid": 123, "cover": "https://example.test/cover.jpg",
                "dynamic": "", "interactive": 0,
            },
            "videos": [
                {"cid": 1, "filename": "old"},
                {"cid": 2, "filename": "new"},
            ],
        }
        payload = publish.build_replacement_edit_payload(
            data,
            item,
            data["videos"][1],
            "https://example.test/new-cover.jpg",
        )
        self.assertEqual(payload["aid"], 123)
        self.assertEqual(payload["cover"], "https://example.test/new-cover.jpg")
        self.assertEqual(payload["tag"], "切片")
        self.assertEqual(
            payload["videos"],
            [{"title": "新版标题", "filename": "new", "desc": ""}],
        )

    def test_execute_uploads_before_removing_old_part(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _item, plan, cookie = self.make_context(root)
            old = {"cid": 1, "filename": "old", "title": "旧版", "desc": ""}
            new = {"cid": 2, "filename": "new", "title": "新版标题", "desc": ""}
            archive = {
                "aid": 123, "cover": "https://example.test/cover.jpg",
                "dynamic": "",
            }
            events = []
            snapshots = iter([
                {"archive": archive, "videos": [old]},
                {"archive": archive, "videos": [old, new]},
                {"archive": archive, "videos": [new]},
            ])

            def get_archive(_bvid, _cookie):
                events.append("get")
                return next(snapshots)

            def run(command, **_kwargs):
                events.append("append")
                self.assertIn("append", command)
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            def edit(_cookie, _csrf, payload):
                events.append("edit")
                self.assertEqual(payload["videos"][0]["filename"], "new")
                return {"code": 0}

            result = publish.execute_replacements(
                "biliup", cookie, plan, 3, "", "web", cookie,
                run=run, get_archive=get_archive, edit_archive=edit,
                upload_cover=lambda *_args: "https://example.test/new-cover.jpg",
                sleep=lambda _seconds: None,
            )
            self.assertEqual(result, 0)
            self.assertLess(events.index("append"), events.index("edit"))
            receipt = json.loads(
                (root / "replacement-receipts.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                receipt["items"]["001"]["new_video"]["filename"], "new"
            )

    def test_append_failure_never_calls_destructive_edit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _item, plan, cookie = self.make_context(root)
            old_snapshot = {
                "archive": {"aid": 123, "cover": "https://example.test/c.jpg"},
                "videos": [{"cid": 1, "filename": "old"}],
            }
            edit = Mock()
            with self.assertRaisesRegex(RuntimeError, "旧稿件未被删除"):
                publish.execute_replacements(
                    "biliup", cookie, plan, 3, "", "web", cookie,
                    run=lambda *_args, **_kwargs: SimpleNamespace(
                        returncode=1, stdout="", stderr="failed"
                    ),
                    get_archive=lambda *_args: old_snapshot,
                    edit_archive=edit,
                    upload_cover=lambda *_args: "https://example.test/new-cover.jpg",
                sleep=lambda _seconds: None,
                )
            edit.assert_not_called()

    def test_interrupted_edit_resumes_without_uploading_again(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _item, plan, cookie = self.make_context(root)
            old = {"cid": 1, "filename": "old"}
            new = {"cid": 2, "filename": "new"}
            archive = {"aid": 123, "cover": "https://example.test/c.jpg"}
            first_snapshots = iter([
                {"archive": archive, "videos": [old]},
                {"archive": archive, "videos": [old, new]},
            ])
            uploads = Mock(return_value=SimpleNamespace(
                returncode=0, stdout="", stderr=""
            ))
            with self.assertRaisesRegex(RuntimeError, "edit failed"):
                publish.execute_replacements(
                    "biliup", cookie, plan, 3, "", "web", cookie,
                    run=uploads,
                    get_archive=lambda *_args: next(first_snapshots),
                    edit_archive=Mock(side_effect=RuntimeError("edit failed")),
                    upload_cover=lambda *_args: "https://example.test/new-cover.jpg",
                sleep=lambda _seconds: None,
                )

            recovery_snapshots = iter([
                {"archive": archive, "videos": [old, new]},
                {"archive": archive, "videos": [new]},
            ])
            publish.execute_replacements(
                "biliup", cookie, plan, 3, "", "web", cookie,
                run=uploads,
                get_archive=lambda *_args: next(recovery_snapshots),
                edit_archive=Mock(return_value={"code": 0}),
                upload_cover=lambda *_args: "https://example.test/new-cover.jpg",
                sleep=lambda _seconds: None,
            )
            self.assertEqual(uploads.call_count, 1)

    def test_retry_cover_cache_tracks_image_bytes_without_reuploading_video(self):
        for change in ("unchanged", "changed", "legacy", "single-new-video"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                item, plan, cookie = self.make_context(root)
                old = {"cid": 1, "filename": "old"}
                new = {"cid": 2, "filename": "new"}
                archive = {"aid": 123, "cover": "https://example.test/old.jpg"}
                snapshots = iter([
                    {"archive": archive, "videos": [old]},
                    {"archive": archive, "videos": [old, new]},
                ])
                uploads = Mock(return_value=SimpleNamespace(returncode=0, stdout="", stderr=""))
                cover_upload = Mock(side_effect=["https://example.test/first.jpg", "https://example.test/repaired.jpg"])
                with self.assertRaisesRegex(RuntimeError, "edit failed"):
                    publish.execute_replacements(
                        "biliup", cookie, plan, 3, "", "web", cookie,
                        run=uploads, get_archive=lambda *_args: next(snapshots),
                        edit_archive=Mock(side_effect=RuntimeError("edit failed")),
                        upload_cover=cover_upload, sleep=lambda _seconds: None,
                    )
                if change in {"changed", "single-new-video"}:
                    stat = item["cover"].stat()
                    item["cover"].write_bytes(b"COVER")
                    os.utime(item["cover"], ns=(stat.st_atime_ns, stat.st_mtime_ns))
                elif change == "legacy":
                    journal_path = root / ".replacement-state.json"
                    journal = json.loads(journal_path.read_text(encoding="utf-8"))
                    journal["items"]["001"].pop("cover_sha256")
                    journal_path.write_text(json.dumps(journal), encoding="utf-8")
                snapshots = iter([
                    {"archive": archive, "videos": [new] if change == "single-new-video" else [old, new]},
                    {"archive": archive, "videos": [new]},
                ])
                edit = Mock(return_value={"code": 0})
                publish.execute_replacements(
                    "biliup", cookie, plan, 3, "", "web", cookie,
                    run=uploads, get_archive=lambda *_args: next(snapshots), edit_archive=edit,
                    upload_cover=cover_upload, sleep=lambda _seconds: None,
                )
                self.assertEqual(uploads.call_count, 1)
                self.assertEqual(cover_upload.call_count, 1 if change == "unchanged" else 2)
                self.assertEqual(edit.call_args.args[2]["cover"],
                                 "https://example.test/first.jpg" if change == "unchanged" else "https://example.test/repaired.jpg")
                receipt = json.loads((root / "replacement-receipts.json").read_text(encoding="utf-8"))
                self.assertEqual(receipt["items"]["001"]["cover_sha256"], publish.sha256_file(item["cover"]))

    def test_completed_replacement_repairs_changed_cover_then_remains_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            item, plan, cookie = self.make_context(root)
            new = {"cid": 2, "filename": "new"}
            old_hash = publish.sha256_file(item["cover"])
            receipt = {"items": {"001": {
                "bvid": plan[0]["bvid"], "source_signature": publish._source_signature(item),
                "new_video": new, "cover_url": "https://example.test/old.jpg", "cover_sha256": old_hash,
            }}}
            (root / "replacement-receipts.json").write_text(json.dumps(receipt), encoding="utf-8")
            item["cover"].write_bytes(b"repaired cover")
            uploads = Mock(side_effect=AssertionError("the existing new video must not be appended again"))
            archive = Mock(return_value={"archive": {"aid": 123}, "videos": [new]})
            edit = Mock(return_value={"code": 0})
            cover_upload = Mock(return_value="https://example.test/repaired.jpg")
            for _ in range(2):
                publish.execute_replacements(
                    "biliup", cookie, plan, 3, "", "web", cookie,
                    run=uploads, get_archive=archive, edit_archive=edit,
                    upload_cover=cover_upload, sleep=lambda _seconds: None,
                )
            uploads.assert_not_called()
            edit.assert_called_once()
            cover_upload.assert_called_once()
            self.assertEqual(edit.call_args.args[2]["cover"], "https://example.test/repaired.jpg")
            self.assertEqual(len(edit.call_args.args[2]["videos"]), 1)

    def test_repaired_cover_upload_failure_does_not_edit_with_cached_old_url(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            item, plan, cookie = self.make_context(root)
            new = {"cid": 2, "filename": "new"}
            saved = {
                "phase": "appended", "bvid": plan[0]["bvid"],
                "source_signature": publish._source_signature(item), "new_video": new,
                "cover_url": "https://example.test/old.jpg", "cover_sha256": "stale",
            }
            (root / ".replacement-state.json").write_text(json.dumps({"items": {"001": saved}}), encoding="utf-8")
            edit = Mock()
            uploads = Mock(side_effect=AssertionError("do not append the existing video"))
            with self.assertRaisesRegex(RuntimeError, "cover failed"):
                publish.execute_replacements(
                    "biliup", cookie, plan, 3, "", "web", cookie,
                    run=uploads, get_archive=lambda *_args: {"archive": {"aid": 123}, "videos": [new]},
                    edit_archive=edit, upload_cover=Mock(side_effect=RuntimeError("cover failed")),
                    sleep=lambda _seconds: None,
                )
            edit.assert_not_called()
            uploads.assert_not_called()


if __name__ == "__main__":
    unittest.main()