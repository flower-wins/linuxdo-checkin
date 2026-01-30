"""
cron: 0 */6 * * *
new Env("Linux.Do 签到（纯HTTP拟人浏览-更多读帖版）")
"""

import os
import random
import time
import functools
import re
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

BROWSE_ENABLED = os.environ.get("BROWSE_ENABLED", "true").strip().lower() not in ["false", "0", "off"]

# ========== 读帖量参数（重点）==========
VISIT_LOOPS = int(os.environ.get("VISIT_LOOPS", "30"))              # 列表->进帖 循环次数
MAX_TOPIC_VISITS = int(os.environ.get("MAX_TOPIC_VISITS", "50"))    # 最多访问多少个不同 topic

# 每个 topic “读回复”的强度（会额外请求 posts.json）
READ_REPLIES_PROB = float(os.environ.get("READ_REPLIES_PROB", "0.80"))
READ_MIN_POSTS = int(os.environ.get("READ_MIN_POSTS", "20"))        # 每个topic最少额外读多少楼（从stream里抽）
READ_MAX_POSTS = int(os.environ.get("READ_MAX_POSTS", "80"))        # 每个topic最多额外读多少楼
POST_IDS_BATCH = int(os.environ.get("POST_IDS_BATCH", "20"))        # 每次 posts.json 带多少 post_ids[]；Discourse 常见做法是 20 [web:24]

# 分布/行为概率
CATEGORY_ROAM_PROB = float(os.environ.get("CATEGORY_ROAM_PROB", "0.25"))
PAGINATION_PROB = float(os.environ.get("PAGINATION_PROB", "0.70"))
OPEN_HTML_PROB = float(os.environ.get("OPEN_HTML_PROB", "0.55"))

# 停留时间（整体更短，保证读得多）
SHORT_WAIT = (float(os.environ.get("SHORT_WAIT_MIN", "1.0")), float(os.environ.get("SHORT_WAIT_MAX", "4.0")))
MID_WAIT = (float(os.environ.get("MID_WAIT_MIN", "4.0")), float(os.environ.get("MID_WAIT_MAX", "10.0")))
LONG_WAIT = (float(os.environ.get("LONG_WAIT_MIN", "12.0")), float(os.environ.get("LONG_WAIT_MAX", "35.0")))

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
    if r < 0.65:
        time.sleep(random.uniform(*SHORT_WAIT))
    elif r < 0.93:
        time.sleep(random.uniform(*MID_WAIT))
    else:
        time.sleep(random.uniform(*LONG_WAIT))


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
        self.stats = {
            "topics": 0,
            "list_pages": 0,
            "topic_json": 0,
            "topic_html": 0,
            "posts_json_calls": 0,
            "extra_posts_read": 0,
            "rate_limit_wait_s": 0,
        }

    def _request(self, method, url, *, expect_json=False, headers=None, data=None, params=None, timeout=25, retries=3):
        hdrs = {}
        if headers:
            hdrs.update(headers)

        if expect_json:
            hdrs.setdefault("Accept", "application/json, text/javascript, */*; q=0.01")
        else:
            hdrs.setdefault("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")

        last_err = None
        for _ in range(retries):
            r = self.session.request(
                method, url,
                headers=hdrs,
                data=data,
                params=params,
                timeout=timeout,
                impersonate="chrome110",
            )

            if r.status_code == 429:
                ra = r.headers.get("Retry-After")
                wait_s = int(ra) if (ra and ra.isdigit()) else random.randint(10, 30)
                self.stats["rate_limit_wait_s"] += wait_s
                logger.warning(f"触发 429，等待 {wait_s}s 后重试")
                time.sleep(wait_s + random.uniform(0, 2))
                continue

            if expect_json:
                ctype = (r.headers.get("Content-Type") or "").lower()
                text = (r.text or "").lstrip()
                if r.status_code >= 400:
                    raise RuntimeError(f"HTTP {r.status_code} for JSON url={url} body_snippet={(text[:200] if text else '')}")
                if ("json" not in ctype) and text.startswith("<"):
                    raise RuntimeError(f"返回HTML而非JSON url={url} ctype={ctype} snippet={text[:200]}")
                try:
                    return r.json()
                except Exception as e:
                    last_err = RuntimeError(f"JSON解析失败 url={url} ctype={ctype} err={e} snippet={(r.text or '')[:200]}")
                    time.sleep(1.0 + random.uniform(0, 1.0))
                    continue

            return r

        raise last_err or RuntimeError("request failed")

    @retry(retries=3)
    def get_csrf(self):
        rj = self._request(
            "GET", CSRF_URL,
            expect_json=True,
            headers={"X-Requested-With": "XMLHttpRequest", "Referer": LOGIN_URL},
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

        try:
            cur = self._request("GET", CURRENT_USER_URL, expect_json=True, retries=2)
            logger.info(f"登录成功: {cur.get('current_user', {}).get('username', USERNAME)}")
        except Exception:
            logger.info("登录成功（未能读取 current.json，但 cookie 已写入）")

        self._request("GET", HOME_URL, expect_json=False, retries=2)
        jitter()
        self._request("GET", urljoin(HOME_URL, "latest"), expect_json=False, retries=2)
        return True

    # -------- Discourse list/topic helpers --------
    def get_latest_json(self, url):
        self.stats["list_pages"] += 1
        return self._request("GET", url, expect_json=True)

    def get_latest_first_page(self):
        return self.get_latest_json(urljoin(HOME_URL, "latest.json"))

    def follow_more_topics(self, latest_json):
        # more_topics_url 指向下一页 [web:16]
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
            return urljoin(HOME_URL, f"c/{slug}/{cid}/l/latest.json")
        except Exception:
            return None

    def get_topic_json(self, topic_id: int):
        self.stats["topic_json"] += 1
        # /t/{id}.json 默认只返回 20 楼左右；stream 里有全部 post id [web:24]
        return self._request("GET", urljoin(HOME_URL, f"t/{topic_id}.json"), expect_json=True, retries=3)

    def get_specific_posts(self, topic_id: int, post_ids):
        self.stats["posts_json_calls"] += 1
        base = urljoin(HOME_URL, f"t/{topic_id}/posts.json")
        qs = urlencode([("post_ids[]", str(pid)) for pid in post_ids])
        return self._request("GET", f"{base}?{qs}", expect_json=True, retries=3)

    def maybe_open_topic_html(self, topic_id: int, slug: str):
        if random.random() > OPEN_HTML_PROB:
            return
        self.stats["topic_html"] += 1
        self._request("GET", urljoin(HOME_URL, f"t/{slug}/{topic_id}"), expect_json=False, retries=2)
        time.sleep(random.uniform(0.8, 2.8))

    def read_more_replies(self, topic_json: dict):
        if random.random() > READ_REPLIES_PROB:
            return
        topic_id = topic_json.get("id")
        ps = topic_json.get("post_stream") or {}
        stream = ps.get("stream") or []
        if not topic_id or not stream:
            return

        # 前 20 个一般已经包含在 topic.json 的 posts 里 [web:24]
        remaining = stream[20:] if len(stream) > 20 else []
        if not remaining:
            return

        want = min(len(remaining), random.randint(READ_MIN_POSTS, READ_MAX_POSTS))
        # 更像真人：偏向读“靠后”的（最新回复），但又夹杂一些随机
        tail_pool = remaining[-300:] if len(remaining) > 300 else remaining
        pick = random.sample(tail_pool, k=min(len(tail_pool), want))

        # 按批次拉取；Discourse 文档示例是 20 个一批 [web:24]
        batch = max(1, POST_IDS_BATCH)
        for i in range(0, len(pick), batch):
            ids = pick[i:i+batch]
            self.get_specific_posts(topic_id, ids)
            self.stats["extra_posts_read"] += len(ids)
            time.sleep(random.uniform(0.8, 3.0))

    def choose_latest_source(self):
        if random.random() < CATEGORY_ROAM_PROB:
            cat_url = self.pick_category_latest_url()
            if cat_url:
                return cat_url
        return urljoin(HOME_URL, "latest.json")

    def browse_like_human(self, loops=VISIT_LOOPS, max_topic_visits=MAX_TOPIC_VISITS):
        topic_visits = 0
        latest_url = self.choose_latest_source()
        try:
            latest_json = self.get_latest_json(latest_url)
        except Exception as e:
            logger.warning(f"首次列表源失败，回退 latest.json: {e}")
            latest_json = self.get_latest_first_page()

        for _ in range(loops):
            human_wait()

            # 翻页：连续跟 1~3 次 more_topics_url（像滚动加载）[web:16]
            if random.random() < PAGINATION_PROB:
                for __ in range(random.randint(1, 3)):
                    more_url = self.follow_more_topics(latest_json)
                    if not more_url:
                        break
                    try:
                        latest_json = self.get_latest_json(more_url)
                        time.sleep(random.uniform(0.8, 2.5))
                    except Exception as e:
                        logger.warning(f"翻页失败，回退首屏: {e}")
                        latest_json = self.get_latest_first_page()
                        break

            topics = self.extract_topics(latest_json)
            if not topics:
                latest_json = self.get_latest_first_page()
                topics = self.extract_topics(latest_json)
                if not topics:
                    raise RuntimeError("无法从 latest.json 获取 topics")

            random.shuffle(topics)
            chosen = None
            for tid, slug, title in topics[:60]:
                if tid not in self.visited_topic_ids:
                    chosen = (tid, slug, title)
                    break
            if not chosen:
                chosen = random.choice(topics)

            tid, slug, title = chosen
            logger.info(f"打开主题: {title} ({tid})")
            self.visited_topic_ids.add(tid)
            self.stats["topics"] += 1

            tj = self.get_topic_json(tid)
            human_wait()

            self.maybe_open_topic_html(tid, slug)
            self.read_more_replies(tj)

            topic_visits += 1
            if topic_visits >= max_topic_visits:
                break

            # 偶尔换“逛的地方”
            if random.random() < 0.25:
                latest_url = self.choose_latest_source()
                try:
                    latest_json = self.get_latest_json(latest_url)
                except Exception:
                    latest_json = self.get_latest_first_page()
                jitter()

        return True

    def send_notifications(self, browse_ok: bool):
        msg = f"每日登录成功: {USERNAME}"
        if BROWSE_ENABLED:
            msg += " + 拟人浏览完成" if browse_ok else " + 拟人浏览失败"
        msg += f"\nTopics: {self.stats['topics']}, list_pages: {self.stats['list_pages']}, topic_json: {self.stats['topic_json']}, posts_calls: {self.stats['posts_json_calls']}, extra_posts: {self.stats['extra_posts_read']}, rl_wait_s: {self.stats['rate_limit_wait_s']}"

        if GOTIFY_URL and GOTIFY_TOKEN:
            try:
                r = requests.post(
                    f"{GOTIFY_URL}/message",
                    params={"token": GOTIFY_TOKEN},
                    json={"title": "LINUX DO", "message": msg, "priority": 1},
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
                r = requests.get(url, params={"title": "LINUX DO", "desp": msg}, timeout=10)
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
