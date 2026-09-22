import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import workflow_auto as auto
import workflow_app as ui

class ManualPriorityMonitorTests(unittest.TestCase):
    def test_deferred_job_returns_to_queue_after_manual_owner_finishes(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            config={"_output_root":root,"_state_file":root/"state.json"}
            state=auto.empty_state()
            state["jobs"]={"a":{"status":"queued","current_stage":"手动流程优先，自动任务已避让"},"b":{"status":"paused","current_stage":"手动流程优先，自动任务已避让"}}
            marker=auto.manual_activity_path(config)
            auto.core.atomic_json(marker,{"pid":os.getpid(),"flows":["manual:flow3"]})
            self.assertEqual(auto.clear_finished_manual_deferrals(config,state),0)
            marker.unlink()
            self.assertEqual(auto.clear_finished_manual_deferrals(config,state),1)
            self.assertEqual(state["jobs"]["a"]["current_stage"],"等待队列继续")
            self.assertEqual(state["jobs"]["b"]["status"],"paused")
            self.assertEqual(auto.clear_finished_manual_deferrals(config,state),0)

    def test_ui_distinguishes_active_manual_work_from_stopped_monitor(self):
        job={"status":"queued","current_stage":"手动流程优先，自动任务已避让"}
        self.assertIn("手动任务",ui.deferred_queue_display(job,manual_active=True,monitor_running=True)[0])
        self.assertIn("等待队列",ui.deferred_queue_display(job,manual_active=False,monitor_running=True)[0])
        stage,detail=ui.deferred_queue_display(job,manual_active=False,monitor_running=False)
        self.assertIn("监控已停止",stage)
        self.assertIn("没有手动任务",detail)
        self.assertIsNone(ui.deferred_queue_display({"status":"paused"},manual_active=False,monitor_running=False))

    def test_watch_survives_transient_file_error_and_releases_locks_on_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/"recordings").mkdir()
            config_path=root/"config.json"
            config_path.write_text(json.dumps({"version":auto.CONFIG_VERSION,"mode":"narrative","state_file":str(root/"projects/state.json"),"watch_root":str(root/"recordings"),"output_root":str(root/"projects"),"read_mikufans_status":False,"allow_burn":False,"allow_upload":False}),encoding="utf-8")
            with patch.object(sys,"argv",["workflow_auto.py","--config",str(config_path),"watch"]),patch.object(auto,"run_once",side_effect=[PermissionError("temporary reader conflict"),KeyboardInterrupt]) as run,patch.object(auto.time,"sleep") as sleep,patch("builtins.print") as output:
                self.assertEqual(auto.main(),130)
            self.assertEqual(run.call_count,2)
            self.assertEqual(sleep.call_count,1)
            self.assertTrue(any("监控保持运行" in str(c) for c in output.call_args_list))
            self.assertEqual(list((root/"projects").glob("*.lock")),[])

if __name__=="__main__":unittest.main()
