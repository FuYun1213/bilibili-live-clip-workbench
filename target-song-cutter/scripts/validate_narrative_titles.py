from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path


TAG_RE = re.compile(r"【[^】]+】")
PREFIX_RE = re.compile(r"^(?:爆了|绷不住了|彻底疯狂|这下爆了|没想到|离谱|震惊)[！!，,：:\s]*")
PRONOUN_RE = re.compile(r"(?:^|[，,；;。！？!?：:\s])(?:她们|他们|她|他)(?=刚|就|却|又|还|被|把|说|想|要|能|不|当|连|从|在)")
KNOWN_SUBJECT_RE = re.compile(r"枝堇|小松绿|羽啾|柚雨|昼夜|灰泽满|十六萤")
ACTION_RE = re.compile(
    r"被|把|说|看|听|买|卖|养|播|下播|取关|拉黑|梦见|跳|飞|摔|退坑|承诺|拒绝|发现|"
    r"催|赶|逼|劝|吓|躲|放|炸|认|做|带|问|念|搜|吃|端|打伞|赶路|强塞|退货|锐评|"
    r"感叹|吐槽|爆料|怒斥|要求|警告|承认|喜欢|讨厌|坚持|自曝|立下|变成|变得|当场|"
    r"对峙|钻进|钻|冬眠|硬刚|败给|报价|翻车|拆解|教|攥|擦|排|研究|诅咒|导流|"
    r"答应|陪|退款|打叉|沉迷|提醒|宣布|讲明|给出|解释|挑战|唱|推荐|总结|"
    r"判断|认定|决定|抛弃|离开|脑控|宣战|威胁|拉链|上班|工作|辞职|离职|入职|"
    r"交(?:社保|钱|费)|缴(?:社保|钱|费)|二选一|做选择|定规矩|定规则|删|忘|开始|"
    r"早退|先走|准备"
)
STRUCTURED_EVENT_RE = re.compile(
    r"二选一|要么.{1,24}要么|"
    r"(?:不|没|要是|如果|只要|必须).{1,24}(?:就|才|否则|不然)|"
    r"(?:既要|既想).{1,24}(?:又要|还要|也要)|"
    r"(?:规定|规则|条件|选择|方案|要求|决定|选址)[：:，,].{2,}|"
    r"(?:成了|成为|变成|变为|从.{1,24}(?:到|变成|变为)).{1,24}|"
    r"[^！？!?]{2,}[？?].{2,}(?:因为|所以|得|要|会|只能|只会|不能|不救|等)"
)
RESULT_RE = re.compile(
    r"结果|却|下一秒|最后|最终|后来|于是|只好|当场|反而|竟|终于|直接|再也|彻底|让|"
    r"逼得|吓得|变成|变得|全白干|归零|破防|看懵|沉默|就|还|已经|宁愿|也不|再次|"
    r"叮嘱|证明|带开朗|杀回|安全落地|飞了起来|摔了|不敢|没出来|认定|翻车|差点|"
    r"没过多久|辞退|地狱|没踹|可能|才|先研究|过渡到|重写|卡在|失速|当机|缓解|"
    r"收场|决定|代替|限制|劝别|提醒|宣布|讲明|给出|改(?:成|为|选|挑|用|讲)|转(?:入|而|向)"
)
HOOK_RE = re.compile(
    r"[！？!?]|谁才是|别|不觉得|忍不住|威胁|哭|老公|脑控|宣战|没活|嗦面|"
    r"收米|拉链|心音|文盲|人设崩塌|我先走了|怪话|酒店|真正的1|痛衣"
)
GENERIC_PLACEHOLDER_RE = re.compile(
    r"通过完整叙事|完整叙事(?:片段|内容)?|标题为|候选片段|选片结果|精彩切片|"
    r"主播(?:聊|谈|讨论|讲述)(?:某|一件|一些|相关)"
)
TOPIC_ONLY_RE = re.compile(r"(?:话题|讨论|闲聊|杂谈|相关内容)$")
DANGLING_END_RE = re.compile(r"(?:因为|所以|但是|然后|而且|不过|以及|并且|让|把|被|还要|想要)[！!。.]?$")
BAD_START_RE = re.compile(r"^(?:只看|只听|只播|刚|曾|小时候|凌晨|每次|天天|偏在|又|想|要|在|看|听|说|梦见|发现|被|把|买|养|播|连播|越|锐评|自曝|吐槽|扬言|立下|要求|拒绝|喜欢|好心|恰好|能|敢|告诉|为了|从|为|以为|坚称)")

ERROR_MESSAGES = {
    "title body lacks an explicit named subject": "缺少明确主语；请直接写主播名或事件中的具体人物",
    "title body lacks a concrete action or event": "缺少具体动作或事件",
    "title body lacks a consequence, reversal, or reaction": "缺少明确结果、反转或反应",
    "title body lacks a payoff or clickable hook": "缺少结果、反差、怪话或可点击钩子",
    "title body is probably a fragment rather than a complete narrative sentence": "正文像片段，不能独立构成完整叙事句",
    "title is a generic placeholder": "标题仍是占位语或泛泛概括",
    "title contains an English single quote": "含英文单引号",
}


def strip_meta(title: str) -> str:
    return PREFIX_RE.sub("", TAG_RE.sub("", title)).strip(" ，,。.")


def explain_errors(errors: list[str]) -> list[str]:
    return [ERROR_MESSAGES.get(error, error) for error in errors]


def errors_for(title: str) -> list[str]:
    errors: list[str] = []
    has_creator_tag = bool(TAG_RE.search(title))
    body = strip_meta(title)
    first_clause = re.split(r"[，,；;。！？!?：:]", body, maxsplit=1)[0]
    explicit_subject = bool(re.match(r"^(?:[\u4e00-\u9fffA-Za-z0-9·_-]{2,20})(?:刚|曾|小时候|凌晨|每次|天天|偏在|又|想|要|在|看|听|说|梦见|发现|被|把|买|养|播|连播|越|锐评|自曝|吐槽|扬言|立下|要求|拒绝|喜欢|好心|恰好|能|敢|告诉|为了|从|为|陪|报价|教|拆解|突然|答应|攥|擦|排|研究|诅咒|导流)", first_clause))
    explicit_subject = explicit_subject or bool(re.match(r"^(?:弹幕|观众|粉丝|陌生观众|摊贩|同学|表姐|老板|骑手|规则|有声书|女团粉丝|教室空调|学霸姐妹团|武汉人|直播间)", first_clause))
    known_subject = bool(KNOWN_SUBJECT_RE.search(body))
    explicit_subject = explicit_subject or known_subject or has_creator_tag
    if (
        not explicit_subject
        or (BAD_START_RE.search(first_clause) and not (known_subject or has_creator_tag))
        or (PRONOUN_RE.search(first_clause) and not (known_subject or has_creator_tag))
    ):
        errors.append("title body lacks an explicit named subject")
    has_event = bool(ACTION_RE.search(body) or STRUCTURED_EVENT_RE.search(body))
    has_hook = bool(RESULT_RE.search(body) or HOOK_RE.search(body))
    # A concise hook, question, state change or concrete action can carry a
    # title on its own. Requiring both lists rejected natural, non-formulaic copy.
    if not has_event and not has_hook:
        errors.extend(
            [
                "title body lacks a concrete action or event",
                "title body lacks a payoff or clickable hook",
            ]
        )
    elif TOPIC_ONLY_RE.search(body) and not has_hook:
        errors.append("title body lacks a payoff or clickable hook")
    if GENERIC_PLACEHOLDER_RE.search(body):
        errors.append("title is a generic placeholder")
    if len(body) < 6 or DANGLING_END_RE.search(body):
        errors.append("title body is probably a fragment rather than a complete narrative sentence")
    if "'" in title:
        errors.append("title contains an English single quote")
    return errors


def titles_from(path: Path) -> list[tuple[str, str]]:
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return [
            (str(item.get("clip_id", index)), str(item.get("title", "")))
            for index, item in enumerate(data.get("items", []), 1)
            if str(item.get("content_type", "narrative")).strip().lower() != "song"
        ]
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    result: list[tuple[str, str]] = []
    for index, row in enumerate(rows, 1):
        if (row.get("content_type") or "narrative").strip().lower() == "song":
            continue
        clip_id = row.get("clip_id") or str(index)
        for field in ("title", "title_a", "title_b"):
            title = (row.get(field) or "").strip()
            if title:
                result.append((f"{clip_id}:{field}", title))
    return result

def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Validate narrative titles as explicit-subject complete sentences.")
    parser.add_argument(
        "--warn-only",
        action="store_true",
        help="Report title issues for human review without blocking Flow 3.",
    )
    parser.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args()
    failures = 0
    for path in args.paths:
        for clip_id, title in titles_from(path):
            errors = errors_for(title)
            if errors:
                failures += 1
                level = "WARN" if args.warn_only else "FAIL"
                print(f"{level} {path}:{clip_id}: {'; '.join(errors)}\n  {title}")
            else:
                print(f"OK {path}:{clip_id}: {title}")
    print(f"checked={sum(len(titles_from(path)) for path in args.paths)} failures={failures}")
    return 0 if args.warn_only else (1 if failures else 0)

if __name__ == "__main__":
    raise SystemExit(main())
