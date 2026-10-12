"""网络设置：国内行情数据域名一律直连，不走系统代理。

很多电脑设置了 HTTP_PROXY 指向本地代理软件（如 127.0.0.1:7897）。代理软件没开时所有请求都会失败，
代理节点在境外时部分行情接口还会拒绝访问。行情数据都在国内，直连最稳。
"""

import os

DIRECT_DOMAINS = [
    "eastmoney.com", "sina.com.cn", "sinajs.cn", "10jqka.com.cn", "legulegu.com",
    "sse.com.cn", "szse.cn", "bse.cn", "cninfo.com.cn", "csindex.com.cn", "npmmirror.com",
    "qq.com", "gtimg.cn",
    "deepseek.com",  # AI 分析（DeepSeek 服务器在国内）
]


def configure() -> None:
    for var in ("NO_PROXY", "no_proxy"):
        cur = [x.strip() for x in os.environ.get(var, "").split(",") if x.strip()]
        for d in DIRECT_DOMAINS:
            if d not in cur:
                cur.append(d)
        os.environ[var] = ",".join(cur)
