#!/usr/bin/python
# coding:utf-8
"""单测：HTTP 444 风控命中的**判定**（2026-10-15 实测暴露的统计 bug）。

背景（实测，不是假想）：`run_5anchors.py` 与 `load_test.py` 原来都用子串判
`if "444" in line` 来数风控命中。而日志行里**到处都是**恰好含 `444` 的
user_id 与时间戳：

    [ENTER]  [3544429557987695][未知]用户9979 进入了直播间
    [PURCHASE] type=LivePopMessage, ts=1792027354448, purchase_ids=[…]

于是一个**完全干净**的窗口（真 444 = 0）会被数成 50 行命中，凭空造出
「残余 ≈12% 风控」的结论 —— 这正是 2026-10-14 那份 5 主播报告里
`http_444_lines: 10` 的来源（该数字**已作废**，见 modify.md）。

修复有两层：
  1. `liveMan.err_text()` 在 `err.response.status_code == 444` 时打**稳定标记**
     `[风控444]` —— 按状态码判，不依赖 requests 拼出来的英文措辞
     （444 没有 reason phrase，requests 会拼成 `444 Client Error:  for url:`，
     中间两个空格，属于实现细节，不该进判据）
  2. `liveMan.is_risk_control_444()` 是**唯一**判据，同时认新标记与旧措辞
     （旧措辞用于回溯历史日志）

用法：python test/test_risk444_unit.py
"""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8',
                                  errors='replace', line_buffering=True)
except Exception:
    pass

from liveMan import RISK_444_TAG, err_text, is_risk_control_444

_ok = _fail = 0


def check(name, cond, detail=''):
    global _ok, _fail
    if cond:
        _ok += 1
        print(f'  ✅ {name}')
    else:
        _fail += 1
        print(f'  ❌ {name}  {detail}')


# ---------- 1. 真 444 认得出来 ----------
def test_recognizes_real_444():
    print('\n[1] 真 444 → 认')
    real = ('[ERROR] 请求直播间URL错误: 444 Client Error:  for url: '
            'https://live.douyin.com/Z.blues')
    check('历史日志里的旧措辞认得', is_risk_control_444(real) is True)
    check('新标记认得', is_risk_control_444(f'请求直播URL错误: boom {RISK_444_TAG}') is True)


# ---------- 2. 假阳性必须全部排除（这是本 bug 的核心）----------
def test_rejects_false_positives():
    print('\n[2] 只是恰好含 444 的行 → 不认（本 bug 的核心）')
    cases = [
        ('user_id 含 444', '[ENTER] [3544429557987695][未知]用户9979 进入了直播间'),
        ('时间戳含 444', '[PURCHASE] type=LivePopMessage, ts=1792027354448, purchase_ids=[3848222250637590882]'),
        ('弹幕正文含 444', '[CHAT] [111111]某人: 这台手机 4444 元贵吗'),
        ('粉丝数含 444', '[FOLLOW] [111111]某*** 关注了主播 (粉丝数 504449)'),
        ('字段名含 444', '[STATUS] room_id=7444001234567 直播间：正在直播'),
        ('连续 444', '[STATUS] 直播间：已结束.'),
    ]
    for label, line in cases:
        check(f'{label} → 不认', is_risk_control_444(line) is False, f'误认: {line[:60]}')


# ---------- 3. err_text 按状态码打标记 ----------
def test_err_text_marks_by_status_code():
    print('\n[3] err_text 按 status_code 打标记，不按文本')
    import requests
    from requests import HTTPError, Response

    def mk_err(code):
        r = Response()
        r.status_code = code
        return HTTPError(f'{code} Client Error:  for url: https://live.douyin.com/x', response=r)

    t444 = err_text(mk_err(444))
    check('status 444 → 带标记', RISK_444_TAG in t444, t444[:80])
    check('status 444 → 判定为命中', is_risk_control_444(t444) is True)

    t403 = err_text(mk_err(403))
    check('status 403 → **不带**标记', RISK_444_TAG not in t403, t403[:80])
    check('status 403 → 不判为命中', is_risk_control_444(t403) is False)

    # 没有 response 的异常（连接层错误）不该被当成 444
    plain = err_text(ValueError('Connection refused'))
    check('无 response 的异常 → 不判为命中', is_risk_control_444(plain) is False)

    # 措辞里出现 444 但没有 444 状态码 —— 这正是旧口径会误判的形状
    class FakeErr(Exception):
        pass
    weird = err_text(FakeErr('444 Client Error 出现在正文里，但状态码不是 444'))
    check('★ 文本含旧措辞但状态码非 444 → 仍判为命中（兼容历史日志的**有意**行为）',
          is_risk_control_444(weird) is True)


# ---------- 4. 两个消费者用的是同一个判据 ----------
def test_single_source():
    print('\n[4] 判据是单一来源（下游不许自己写子串匹配）')
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for rel in ['service/run_5anchors.py', 'load_test.py']:
        src = io.open(os.path.join(root, rel), encoding='utf-8').read()
        check(f'{rel} 调 is_risk_control_444', 'is_risk_control_444' in src)
        # 只看**代码**行，注释里为了说明而引用 "444" 是可以的
        bad = [l for l in src.splitlines()
               if not l.lstrip().startswith('#')
               and ('"444" in' in l or "'444' in" in l)]
        check(f'{rel} 没有裸子串匹配 `"444" in`', not bad, f'仍有: {bad}')


if __name__ == '__main__':
    print('=' * 64)
    print('HTTP 444 风控判定单测（合成数据，不打网络）')
    print('=' * 64)
    test_recognizes_real_444()
    test_rejects_false_positives()
    test_err_text_marks_by_status_code()
    test_single_source()
    print('\n' + '=' * 64)
    print(f'结果：通过 {_ok} / 失败 {_fail}')
    if _fail:
        print('❌ 444 判定有误 —— 会把普通日志行数成风控命中')
        sys.exit(1)
    print('✅ 444 判定正确（真命中认得、假阳性排除、判据单一来源）')
