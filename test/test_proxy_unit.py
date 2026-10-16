#!/usr/bin/python
# coding:utf-8
"""单测：控制面代理的解析与注入形状（**不打网络**，纯合成数据）。

为什么要单独测这块：代理是「配错了不会报错、只会静默连不上或静默直连」的那类东西。
两种失败模式都很难从现象反推：

  * **静默直连**：env 没读到 / 参数名拼错 / WS 漏传 → 请求照常成功，只是没走代理。
    在数据中心 IP 上这表现为「偶尔 444」，在住宅 IP 上则**完全看不出来**。
  * **位置写错**：ffmpeg 的 `-http_proxy` 若放到 `-i` 之后就是输出选项，对已建好的
    连接无效 —— ffmpeg 不报错，只是没走代理。

用法：python test/test_proxy_unit.py
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

import liveMan

_ok = _fail = 0


def check(name, cond, detail=''):
    global _ok, _fail
    if cond:
        _ok += 1
        print(f'  ✅ {name}')
    else:
        _fail += 1
        print(f'  ❌ {name}  {detail}')


# ⚠️ **合成值**，形状（user:pass@host:port）与真实隧道代理一致，但账密是假的。
# 真凭据只该待在本机/服务器的 config/proxy.env（.gitignore 里），
# **不要**为了让测试「更真」而把它抄进来 —— test/ 是会被提交的。
SAMPLE = 'http://tunneluser:tunnelpass@proxy.example.net:23362'
SAMPLE_HOST = 'proxy.example.net'
SAMPLE_PORT = 23362
SAMPLE_AUTH = ('tunneluser', 'tunnelpass')


def with_env(value, key='DOUYIN_PROXY'):
    """设 env 并把 liveMan 的解析缓存置回未解析（模块常量是解析一次的）"""
    for k in liveMan.PROXY_ENV_KEYS:
        os.environ.pop(k, None)
    if value is not None:
        os.environ[key] = value
    liveMan._PROXY_RESOLVED = False
    return liveMan


def restore():
    for k in liveMan.PROXY_ENV_KEYS:
        os.environ.pop(k, None)
    liveMan._PROXY_RESOLVED = False


def test_parse():
    print('\n[1] parse_proxy 拆解')
    p = liveMan.parse_proxy(SAMPLE)
    check('host 正确', p['host'] == SAMPLE_HOST, p)
    check('port 是 int 不是 str', p['port'] == SAMPLE_PORT and isinstance(p['port'], int), p)
    check('账密拆成 (user, pass) 二元组', p['auth'] == SAMPLE_AUTH, p)
    check('url 原样保留', p['url'] == SAMPLE, p)

    check('省略 scheme 默认 http', liveMan.parse_proxy('1.2.3.4:8080')['url']
          == 'http://1.2.3.4:8080')
    check('无账密时 auth=None', liveMan.parse_proxy('http://h:1')['auth'] is None)
    check('http 无端口默认 80', liveMan.parse_proxy('http://h')['port'] == 80)
    check('https 无端口默认 443', liveMan.parse_proxy('https://h')['port'] == 443)

    # 账密里的特殊字符必须 unquote —— 不 unquote 的话请求会带着 `%3A` 去认证而失败
    q = liveMan.parse_proxy('http://u%40x:p%3Aw@h:1')
    check('账密 percent-decode（u%40x→u@x, p%3Aw→p:w）',
          q['auth'] == ('u@x', 'p:w'), q)

    check('空串 / 空白 / None 都返回 None',
          liveMan.parse_proxy('') is None and liveMan.parse_proxy('   ') is None
          and liveMan.parse_proxy(None) is None)

    # 明确报错而不是静默直连：猜错了会让人以为「代理配了但没用」
    try:
        liveMan.parse_proxy('socks5://h:1')
        check('socks5 明确报错（不静默降级）', False, '没抛异常')
    except ValueError:
        check('socks5 明确报错（不静默降级）', True)
    try:
        liveMan.parse_proxy('http://')
        check('无主机名明确报错', False, '没抛异常')
    except ValueError:
        check('无主机名明确报错', True)


def test_env_resolution():
    print('\n[2] 环境变量解析与优先级')
    with_env(None)
    check('未配置时 requests 实参是**空 dict**（不是 None —— None 会让 requests '
          '回落到自己读 env，行为就不可复现了）',
          liveMan.proxy_for_requests() == {}, liveMan.proxy_for_requests())
    check('未配置代理时 WS 实参是 {} → `run_forever(**{})` 与改动前逐字节等价',
          liveMan.proxy_for_ws() == {})

    with_env(SAMPLE)
    r = liveMan.proxy_for_requests()
    check('requests：http 与 https 都指向同一代理', r.get('http') == SAMPLE
          and r.get('https') == SAMPLE, r)

    # 优先级：DOUYIN_PROXY 优先于 http_proxy（后者是通用变量，可能被别的工具设成别的值）
    with_env('http://other:1', key='http_proxy')
    check('只用 http_proxy 时也能读到（兜底）',
          liveMan.proxy_for_requests().get('http') == 'http://other:1')
    os.environ['DOUYIN_PROXY'] = SAMPLE
    liveMan._PROXY_RESOLVED = False
    check('两个都设时 DOUYIN_PROXY 优先',
          liveMan.proxy_for_requests().get('http') == SAMPLE,
          liveMan.proxy_for_requests())

    with_env('   ')          # 只有空白不算配置
    check('纯空白 env 视为未配置', liveMan.proxy_for_requests() == {})
    restore()


def test_ws_proxy_switch():
    print('\n[4] ★ WebSocket 是**独立**开关，且**默认直连**（2026-10-16 隧道掐长连接）')
    # 隧道在 ~6.5 分钟处掐断长连接（三组对照实测，见 liveMan.WS_PROXY_ENV 的注释）。
    # 所以「配了 DOUYIN_PROXY」**不再**意味着 WS 走隧道 —— 这是本条单测要钉住的核心不变量：
    # 哪天有人为了「对称」把 `ws_proxy_enabled()` 去掉，WS 会重新被掐成 6 分钟碎片。
    with_env(SAMPLE)
    check('★ 配了隧道，但 WS **仍直连**（`proxy_for_ws()` 返回空 dict）',
          liveMan.proxy_for_ws() == {}, liveMan.proxy_for_ws())
    check('同期 HTTP 照旧走隧道（怕 444 的短请求没被牵连）',
          liveMan.proxy_for_requests().get('http') == SAMPLE,
          liveMan.proxy_for_requests())

    os.environ['DOUYIN_WS_PROXY'] = '1'
    w = liveMan.proxy_for_ws()
    check('设 DOUYIN_WS_PROXY=1 才恢复走隧道（换供应商后 A/B 用，不改代码）',
          w.get('http_proxy_host') == SAMPLE_HOST
          and w.get('http_proxy_port') == SAMPLE_PORT, w)
    check('WS：账密以元组传给 http_proxy_auth',
          w.get('http_proxy_auth') == SAMPLE_AUTH, w)
    check('WS：参数名与 websocket-client 的 run_forever 签名一致',
          set(w) == {'http_proxy_host', 'http_proxy_port', 'http_proxy_auth',
                     'proxy_type'}, set(w))
    # 不给 proxy_type 会必报「Only http, socks4, socks5 proxy protocols are supported」：
    # run_forever 的默认值是 None，而它把 None 原样塞进 options，下游
    # `options.get("proxy_type", "http")` 在 key 存在但值为 None 时**拿不到默认值**。
    check('WS：proxy_type 显式为 "http"（缺了必报 ProxyError，实测踩过）',
          w.get('proxy_type') == 'http', w)

    # 假值一律当成「关」—— 运维写 `DOUYIN_WS_PROXY=off` 时不该反而把隧道打开
    for v in ('0', 'no', 'off', 'false', 'OFF', '  ', ''):
        os.environ['DOUYIN_WS_PROXY'] = v
        check(f'DOUYIN_WS_PROXY={v!r} 视为关（直连）',
              liveMan.proxy_for_ws() == {}, liveMan.proxy_for_ws())
    for v in ('1', 'true', 'yes', 'on', 'TRUE'):
        os.environ['DOUYIN_WS_PROXY'] = v
        check(f'DOUYIN_WS_PROXY={v!r} 视为开', bool(liveMan.proxy_for_ws()), v)

    # 开关是**开关**，不是「替代 DOUYIN_PROXY」：没配隧道时开着也还是直连
    os.environ['DOUYIN_WS_PROXY'] = '1'
    with_env(None)
    os.environ['DOUYIN_WS_PROXY'] = '1'
    check('没配 DOUYIN_PROXY 时，开关打开也仍直连（没有隧道可走）',
          liveMan.proxy_for_ws() == {}, liveMan.proxy_for_ws())

    os.environ.pop('DOUYIN_WS_PROXY', None)
    restore()


def test_stream_proxy():
    print('\n[3] 媒体流代理是**独立**开关（默认直连）')
    os.environ.pop('DOUYIN_STREAM_PROXY', None)
    with_env(SAMPLE)
    check('控制面配了代理时，媒体流仍默认直连（两者刻意分开：一路 6.2 Mbps，'
          '5 路 ≈ 31 Mbps，没必要不穿隧道）',
          liveMan.stream_proxy_url() is None, liveMan.stream_proxy_url())
    from liveMan import MediaCapture
    import tempfile
    tmp = tempfile.mkdtemp(prefix='proxy_')
    check('MediaCapture 默认 stream_proxy=None → 命令里无 -http_proxy',
          '-http_proxy' not in MediaCapture('0', tmp, log=lambda *a: None)
          ._merged_cmd('U', 0, 0))

    os.environ['DOUYIN_STREAM_PROXY'] = 'http://sp:1'
    check('设了 DOUYIN_STREAM_PROXY 才走',
          MediaCapture('0', tmp, log=lambda *a: None).stream_proxy == 'http://sp:1')
    os.environ.pop('DOUYIN_STREAM_PROXY', None)
    restore()


if __name__ == '__main__':
    print('=' * 64)
    print('控制面代理单测（解析 + 注入形状，不打网络）')
    print('=' * 64)
    test_parse()
    test_env_resolution()
    test_stream_proxy()
    test_ws_proxy_switch()
    print('\n' + '=' * 64)
    print(f'结果：通过 {_ok} / 失败 {_fail}')
    if _fail:
        print('❌ 代理配置有静默失效路径')
        sys.exit(1)
    print('✅ 解析与注入形状均正确')
