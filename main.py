"""
cron: 0 */6 * * *
new Env("Linux.Do 签到（纯HTTP拟人浏览-增强稳定版）")
"""

import os
import random
import time
import functools
import re
import json
from urllib.parse import urljoin, urlencode

from loguru import logger
from curl_cffi import requests

HOME_URL = "https://linux.do/"
LOGIN_URL = urljoin(HOME_URL, "login")
SESSION_URL = urljoin(HOME_URL, "session")
CSRF_URL = urljoin(HOME_URL, "session/csrf")
CURRENT_USER_URL = urljoin(HOME_URL, "session/current.json")  # 登录态探测 [web:48]

USERNAME = os.environ.get("LINUXDO_USERNAME") or os.environ.get("USERNAME")
PASSWORD = os.environ.get("LINUXDO_PASSWORD") or os.environ.get("PASSWORD")

BROWSE_ENABLED = os.environ.get("BROWSE_ENABLED", "true").strip().lower() not in [
    "false", "0", "off",
]

VISIT_LOOPS = int(os.environ.get("VISIT_LOOPS", "8"))
MAX_TOPIC_VISITS = int(os.environ.get("MAX_TOPIC_VISITS", "12"))
CATEGORY_ROAM_PROB = float(os.environ.get("CATEGORY_ROAM_PROB", "0.25"))
PAGINATION_PROB = float(os.environ.get("PAGINATION_PROB", "0.35"))
OPEN_HTML_PROB = float(os.environ.get("OPEN_HTML_PROB", "0.55"))
READ_REPLIES_PROB = float(os.environ.get("READ_REPLIES_PROB", "0.35"))

GOTIFY_URL = os.environ.get("GOTIFY_URL")
GOTIFY_TOKEN = os.environ.get("GOTIFY_TOKEN")
SC3_PUSH_KEY = os.environ.get("SC3_PUSH_KEY")


def retry(retries=3, base_sleep=1.2):
    def deco(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last = None
            for i in range(retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last = e
                    logger.warning(f"{func.__name__} 第 {i+1}/{retries} 次失败: {e}")
                    time.sleep(base_sleep + random.uniform(0, 0.8))
            raise last
        return wrapper
    return deco


def human_wait():
    r = random.random()
    if r < 0.6:
        time.sleep(random.uniform(2, 8))
    elif r < 0.9:
        time.sleep(random.uniform(10, 25))
    else:
        time.sleep(random.uniform(30, 90))


def jitter(min_s=0.2, max_s=1.2):
    time.sleep(random.uniform(min_s, max_s))


def pick_user_agent():
    uas = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    ]
    return random.choice(uas)


class LinuxDoHumanBrowser:
    def __init__(self):
        self.session = requests.Session()
        self.ua = pick_user_agent()
        self.session.headers.update({
            "User-Agent": self.ua,
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        self.visited_topic_ids = set()

    def _request(self, method, url, *, expect_json=False, headers=None, data=None, params=None, timeout=25, retries=3):
        """
        更稳健的请求：
        - 处理 429：按 Retry-After 退避重试 [web:49]
        - JSON：检查 Content-Type/响应体，避免直接 .json() 崩
        """
        hdrs = {}
        if headers:
            hdrs.update(headers)

        if expect_json:
            hdrs.setdefault("Accept", "application/json, text/javascript, */*; q=0.01")
        else:
            hdrs.setdefault("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")

        last_err = None
        for attempt in range(retries):
            r = self.session.request(
                method, url,
                headers=hdrs,
                data=data,
                params=params,
                timeout=timeout,
                impersonate="chrome110",
            )

            # 429 rate limit: obey Retry-After if present [web:49]
            if r.status_code == 429:
                ra = r.headers.get("Retry-After")
                wait_s = int(ra) if (ra and ra.isdigit()) else random.randint(10, 30)
                logger.warning(f"触发 429，等待 {wait_s}s 后重试")
                time.sleep(wait_s + random.uniform(0, 2))
                continue

            # 有些防护会返回 200 但内容是 HTML challenge/login 页，导致 JSONDecodeError
            if expect_json:
                ctype = (r.headers.get("Content-Type") or "").lower()
                text = (r.text or "").lstrip()
                if r.status_code >= 400:
                    snippet = (text[:200] if text else "")
                    raise RuntimeError(f"HTTP {r.status_code} for JSON url={url} body_snippet={snippet}")

                # Content-Type 不像 JSON 且 body 以 < 开头，多半是 HTML
                if ("json" not in ctype) and text.startswith("<"):
                    snippet = text[:200]
                    raise RuntimeError(f"返回HTML而非JSON url={url} ctype={ctype} snippet={snippet}")

                try:
                    return r.json()
                except Exception as e:
                    snippet = (r.text or "")[:200]
                    last_err = RuntimeError(f"JSON解析失败 url={url} ctype={ctype} err={e} snippet={snippet}")
                    time.sleep(1.0 + random.uniform(0, 1.0))
                    continue

            return r

        raise last_err or RuntimeError("request failed")

    @retry(retries=3)
    def get_csrf(self):
        rj = self._request(
            "GET", CSRF_URL,
            expect_json=True,
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Referer": LOGIN_URL,
            },
        )
        token = rj.get("csrf")
        if not token:
            raise RuntimeError("CSRF token missing")
        return token

    @retry(retries=3)
    def login(self) -> bool:
        logger.info("开始登录")
        csrf = self.get_csrf()

        rj = self._request(
            "POST", SESSION_URL,
            expect_json=True,
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Referer": LOGIN_URL,
                "X-CSRF-Token": csrf,
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Origin": "https://linux.do",
            },
            data={
                "login": USERNAME,
                "password": PASSWORD,
                "second_factor_method": "1",
                "timezone": "Asia/Shanghai",
            },
        )
        if rj.get("error"):
            logger.error(f"登录失败: {rj.get('error')}")
            return False

        # 登录态探测：/session/current.json（已登录 200，未登录常见 404）[web:48]
        try:
            cur = self._request("GET", CURRENT_USER_URL, expect_json=True, retries=2)
            logger.info(f"登录成功: {cur.get('current_user', {}).get('username', USERNAME)}")
        except Exception:
            logger.info("登录成功（未能读取 current.json，但 cookie 已写入）")

        # 热身
        self._request("GET", HOME_URL, expect_json=False, retries=2)
        jitter()
        self._request("GET", urljoin(HOME_URL, "latest"), expect_json=False, retries=2)
        return True

    def get_latest_json(self, url):
        return self._request("GET", url, expect_json=True)

    def get_latest_first_page(self):
        return self.get_latest_json(urljoin(HOME_URL, "latest.json"))

    def follow_more_topics(self, latest_json):
        # more_topics_url 用于继续加载列表（滚动加载/翻页线索）[web:16]
        tl = latest_json.get("topic_list") or {}
        more = tl.get("more_topics_url")
        if not more:
            return None
        return urljoin(HOME_URL, more.lstrip("/"))

    def extract_topics(self, latest_json):
        tl = latest_json.get("topic_list") or {}
        topics = tl.get("topics") or []
        out = []
        for t in topics:
            tid = t.get("id")
            slug = t.get("slug")
            title = t.get("title") or ""
            if tid and slug:
                out.append((tid, slug, title))
        return out

    def get_categories_json(self):
        return self._request("GET", urljoin(HOME_URL, "categories.json"), expect_json=True, retries=2)

    def pick_category_latest_url(self):
        try:
            j = self.get_categories_json()
            cats = (j.get("category_list") or {}).get("categories") or []
            if not cats:
                return None
            c = random.choice(cats)
            slug = c.get("slug")
            cid = c.get("id")
            if not slug or not cid:
                return None
            # 分类 latest JSON：/c/{slug}/{id}/l/latest.json [web:16]
            return urljoin(HOME_URL, f"c/{slug}/{cid}/l/latest.json")
        except Exception:
            return None

    def get_topic_json(self, topic_id: int):
        # 用 /t/{id}.json；更稳（不依赖 slug）[web:24]
        return self._request("GET", urljoin(HOME_URL, f"t/{topic_id}.json"), expect_json=True, retries=3)

    def get_specific_posts(self, topic_id: int, post_ids):
        # 抽取部分 post（像在看评论）[web:24]
        base = urljoin(HOME_URL, f"t/{topic_id}/posts.json")
        qs = urlencode([("post_ids[]", str(pid)) for pid in post_ids])
        return self._request("GET", f"{base}?{qs}", expect_json=True, retries=3)

    def maybe_open_topic_html(self, topic_id: int, slug: str):
        if random.random() > OPEN_HTML_PROB:
            return
        self._request("GET", urljoin(HOME_URL, f"t/{slug}/{topic_id}"), expect_json=False, retries=2)
        time.sleep(random.uniform(1, 4))

    def maybe_read_some_replies(self, topic_json: dict):
        if random.random() > READ_REPLIES_PROB:
            return
        topic_id = topic_json.get("id")
        ps = topic_json.get("post_stream") or {}
        stream = ps.get("stream") or []
        if not topic_id or not stream:
            return
        tail = stream[2:] if len(stream) > 2 else stream
        if not tail:
            return
        k = min(len(tail), random.randint(3, 10))
        pick = random.sample(tail, k=k)
        self.get_specific_posts(topic_id, pick)
        time.sleep(random.uniform(1, 4))

    def choose_latest_source(self):
        if random.random() < CATEGORY_ROAM_PROB:
            cat_url = self.pick_category_latest_url()
            if cat_url:
                return cat_url
        return urljoin(HOME_URL, "latest.json")

    def browse_like_human(self, loops=VISIT_LOOPS, max_topic_visits=MAX_TOPIC_VISITS):
        topic_visits = 0
        latest_url = self.choose_latest_source()

        # 首次拉列表失败就回退 latest.json 首屏
        try:
            latest_json = self.get_latest_json(latest_url)
        except Exception as e:
            logger.warning(f"首次列表源失败，回退 latest.json: {e}")
            latest_json = self.get_latest_first_page()

        for _ in range(loops):
            human_wait()

            if random.random() < PAGINATION_PROB:
                more_url = self.follow_more_topics(latest_json)
                if more_url:
                    try:
                        latest_json = self.get_latest_json(more_url)
                        time.sleep(random.uniform(1, 3))
                    except Exception as e:
                        logger.warning(f"翻页失败，回退首屏: {e}")
                        latest_json = self.get_latest_first_page()

            topics = self.extract_topics(latest_json)
            if not topics:
                latest_json = self.get_latest_first_page()
                topics = self.extract_topics(latest_json)
                if not topics:
                    raise RuntimeError("无法从 latest.json 获取 topics")

            # 更像真人：优先选未访问过的
            random.shuffle(topics)
            chosen = None
            for tid, slug, title in topics[:40]:
                if tid not in self.visited_topic_ids:
                    chosen = (tid, slug, title)
                    break
            if not chosen:
                chosen = random.choice(topics)

            tid, slug, title = chosen
            logger.info(f"打开主题: {title} ({tid})")
            self.visited_topic_ids.add(tid)

            tj = self.get_topic_json(tid)
            human_wait()

            self.maybe_open_topic_html(tid, slug)
            self.maybe_read_some_replies(tj)

            topic_visits += 1
            if topic_visits >= max_topic_visits:
                break

            if random.random() < 0.25:
                latest_url = self.choose_latest_source()
                try:
                    latest_json = self.get_latest_json(latest_url)
                except Exception:
                    latest_json = self.get_latest_first_page()
                jitter()

        return True

    def send_notifications(self, browse_ok: bool):
        status_msg = f"每日登录成功: {USERNAME}"
        if BROWSE_ENABLED:
            status_msg += " + 拟人浏览完成" if browse_ok else " + 拟人浏览失败"

        if GOTIFY_URL and GOTIFY_TOKEN:
            try:
                r = requests.post(
                    f"{GOTIFY_URL}/message",
                    params={"token": GOTIFY_TOKEN},
                    json={"title": "LINUX DO", "message": status_msg, "priority": 1},
                    timeout=10,
                )
                r.raise_for_status()
                logger.success("Gotify 推送成功")
            except Exception as e:
                logger.error(f"Gotify 推送失败: {e}")

        if SC3_PUSH_KEY:
            m = re.match(r"sct(\d+)t", SC3_PUSH_KEY, re.I)
            if not m:
                logger.error("SC3_PUSH_KEY 格式错误，未获取到UID")
                return
            uid = m.group(1)
            url = f"https://{uid}.push.ft07.com/send/{SC3_PUSH_KEY}"
            try:
                r = requests.get(url, params={"title": "LINUX DO", "desp": status_msg}, timeout=10)
                r.raise_for_status()
                logger.success(f"Server酱³ 推送成功: {r.text}")
            except Exception as e:
                logger.error(f"Server酱³ 推送失败: {e}")

    def run(self):
        if not self.login():
            self.send_notifications(False)
            return

        browse_ok = True
        if BROWSE_ENABLED:
            try:
                browse_ok = self.browse_like_human()
                logger.info("拟人浏览任务完成")
            except Exception as e:
                browse_ok = False
                logger.error(f"拟人浏览异常: {e}")

        self.send_notifications(browse_ok)


if __name__ == "__main__":
    if not USERNAME or not PASSWORD:
        raise SystemExit("Please set USERNAME/PASSWORD or LINUXDO_USERNAME/LINUXDO_PASSWORD")
    LinuxDoHumanBrowser().run()
