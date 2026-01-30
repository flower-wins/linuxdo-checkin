"""
cron: 0 */6 * * *
new Env("Linux.Do 签到（纯HTTP拟人浏览）")
"""

import os
import random
import time
import functools
import re
from urllib.parse import urljoin, urlencode

from loguru import logger
from curl_cffi import requests

# ---------------- Config ----------------
HOME_URL = "https://linux.do/"
LOGIN_URL = urljoin(HOME_URL, "login")
SESSION_URL = urljoin(HOME_URL, "session")
CSRF_URL = urljoin(HOME_URL, "session/csrf")

USERNAME = os.environ.get("LINUXDO_USERNAME") or os.environ.get("USERNAME")
PASSWORD = os.environ.get("LINUXDO_PASSWORD") or os.environ.get("PASSWORD")

BROWSE_ENABLED = os.environ.get("BROWSE_ENABLED", "true").strip().lower() not in [
    "false", "0", "off",
]

# 拟人浏览参数（可用环境变量覆盖）
VISIT_LOOPS = int(os.environ.get("VISIT_LOOPS", "8"))              # 列表->帖子 循环次数
MAX_TOPIC_VISITS = int(os.environ.get("MAX_TOPIC_VISITS", "12"))   # 最多访问多少个不同 topic
CATEGORY_ROAM_PROB = float(os.environ.get("CATEGORY_ROAM_PROB", "0.25"))  # 逛分类页概率
PAGINATION_PROB = float(os.environ.get("PAGINATION_PROB", "0.35"))        # 列表翻页概率
OPEN_HTML_PROB = float(os.environ.get("OPEN_HTML_PROB", "0.55"))          # topic 再开 HTML 概率
READ_REPLIES_PROB = float(os.environ.get("READ_REPLIES_PROB", "0.35"))    # 抽取部分回复概率

GOTIFY_URL = os.environ.get("GOTIFY_URL")
GOTIFY_TOKEN = os.environ.get("GOTIFY_TOKEN")
SC3_PUSH_KEY = os.environ.get("SC3_PUSH_KEY")

# ---------------- Utils ----------------
def retry(retries=3, sleep=1.2):
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
                    time.sleep(sleep)
            raise last
        return wrapper
    return deco

def human_wait():
    """多数短停留，少数长停留（长尾分布）。"""
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
    # 不要每次都变UA；但可在启动时随机一次
    uas = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    ]
    return random.choice(uas)

# ---------------- Core ----------------
class LinuxDoHumanBrowser:
    def __init__(self):
        self.session = requests.Session()
        self.ua = pick_user_agent()
        self.session.headers.update({
            "User-Agent": self.ua,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        self.visited_topic_ids = set()

    @retry(retries=3)
    def get_csrf(self):
        headers = {
            "User-Agent": self.ua,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": LOGIN_URL,
        }
        r = self.session.get(CSRF_URL, headers=headers, impersonate="chrome110")
        r.raise_for_status()
        token = r.json().get("csrf")
        if not token:
            raise RuntimeError("CSRF token missing")
        return token

    @retry(retries=3)
    def login(self) -> bool:
        logger.info("开始登录")
        csrf = self.get_csrf()

        headers = {
            "User-Agent": self.ua,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": LOGIN_URL,
            "X-CSRF-Token": csrf,
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": "https://linux.do",
        }
        data = {
            "login": USERNAME,
            "password": PASSWORD,
            "second_factor_method": "1",
            "timezone": "Asia/Shanghai",
        }
        r = self.session.post(SESSION_URL, data=data, headers=headers, impersonate="chrome110")
        r.raise_for_status()
        j = r.json()
        if j.get("error"):
            logger.error(f"登录失败: {j.get('error')}")
            return False

        # 访问首页/最新页“热身”一下
        self.session.get(HOME_URL, impersonate="chrome110")
        jitter()
        self.session.get(urljoin(HOME_URL, "latest"), impersonate="chrome110")
        logger.info("登录成功")
        return True

    @retry(retries=3)
    def get_latest_json(self, url: str):
        r = self.session.get(url, impersonate="chrome110")
        r.raise_for_status()
        return r.json()

    def get_latest_first_page(self):
        return self.get_latest_json(urljoin(HOME_URL, "latest.json"))

    def follow_more_topics(self, latest_json):
        # Discourse 的分页线索在 topic_list.more_topics_url [web:16]
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

    @retry(retries=3)
    def get_categories_json(self):
        # categories.json 常见于 Discourse；如果站点关闭也不会影响（失败则回退 latest）[web:6]
        r = self.session.get(urljoin(HOME_URL, "categories.json"), impersonate="chrome110")
        r.raise_for_status()
        return r.json()

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
            # 分类 latest JSON：/c/{slug}/{id}/l/latest.json [web:6]
            return urljoin(HOME_URL, f"c/{slug}/{cid}/l/latest.json")
        except Exception:
            return None

    @retry(retries=3)
    def get_topic_json(self, topic_id: int, slug: str, print_mode=False):
        # /t/{id}.json 是单 topic；print=true 可把返回 chunk_size 提到 1000（不要常用，太“机器人”）[web:24]
        qs = "?print=true" if print_mode else ""
        url = urljoin(HOME_URL, f"t/{topic_id}.json{qs}")
        r = self.session.get(url, impersonate="chrome110")
        r.raise_for_status()
        return r.json()

    @retry(retries=3)
    def get_specific_posts(self, topic_id: int, post_ids):
        # 通过 /t/{id}/posts.json + post_ids[] 抽取部分回复 [web:24]
        base = urljoin(HOME_URL, f"t/{topic_id}/posts.json")
        qs = urlencode([("post_ids[]", str(pid)) for pid in post_ids])
        r = self.session.get(f"{base}?{qs}", impersonate="chrome110")
        r.raise_for_status()
        return r.json()

    def maybe_open_topic_html(self, topic_id: int, slug: str):
        if random.random() > OPEN_HTML_PROB:
            return
        # HTML 打开更像真人（但仍是 GET）
        url = urljoin(HOME_URL, f"t/{slug}/{topic_id}")
        self.session.get(url, impersonate="chrome110")
        time.sleep(random.uniform(1, 4))

    def maybe_read_some_replies(self, topic_json: dict):
        if random.random() > READ_REPLIES_PROB:
            return
        topic_id = topic_json.get("id")
        ps = topic_json.get("post_stream") or topic_json.get("posts_stream") or {}
        stream = ps.get("stream") or []
        if not topic_id or not stream:
            return

        # topic.json 默认只含前 20 条左右；stream 是全部 post id 列表 [web:24]
        # 为了“像人”，随机挑 3~12 个 post id（避开前 1-2 个）
        tail = stream[2:] if len(stream) > 2 else stream
        if not tail:
            return
        k = min(len(tail), random.randint(3, 12))
        pick = random.sample(tail, k=k)
        self.get_specific_posts(topic_id, pick)
        time.sleep(random.uniform(1, 4))

    def choose_latest_source(self):
        # 有一定概率逛分类，否则逛全站 latest
        if random.random() < CATEGORY_ROAM_PROB:
            cat_url = self.pick_category_latest_url()
            if cat_url:
                return cat_url
        return urljoin(HOME_URL, "latest.json")

    def browse_like_human(self, loops=VISIT_LOOPS, max_topic_visits=MAX_TOPIC_VISITS):
        topic_visits = 0
        latest_url = self.choose_latest_source()
        latest_json = self.get_latest_json(latest_url)

        for _ in range(loops):
            # 1) 列表页停留
            human_wait()

            # 2) 有概率翻页（跟随 more_topics_url 更自然）[web:16]
            if random.random() < PAGINATION_PROB:
                more_url = self.follow_more_topics(latest_json)
                if more_url:
                    latest_json = self.get_latest_json(more_url)
                    time.sleep(random.uniform(1, 3))

            topics = self.extract_topics(latest_json)
            if not topics:
                # 回退到首页 latest
                latest_json = self.get_latest_first_page()
                topics = self.extract_topics(latest_json)
                if not topics:
                    logger.error("无法从 latest.json 获取 topics")
                    return False

            # 3) 选 topic：偏向未访问过的
            random.shuffle(topics)
            chosen = None
            for tid, slug, title in topics[:30]:
                if tid not in self.visited_topic_ids:
                    chosen = (tid, slug, title)
                    break
            if not chosen:
                chosen = random.choice(topics)
            tid, slug, title = chosen

            logger.info(f"打开主题: {title} ({tid})")
            self.visited_topic_ids.add(tid)

            # 4) 请求 topic JSON
            tj = self.get_topic_json(tid, slug, print_mode=False)
            human_wait()

            # 5) 可选：再开 HTML、更像“真的点开”
            self.maybe_open_topic_html(tid, slug)

            # 6) 可选：抽取一些回复 id 请求 posts.json（像在翻评论）[web:24]
            self.maybe_read_some_replies(tj)

            topic_visits += 1
            if topic_visits >= max_topic_visits:
                break

            # 7) 偶尔“换地方逛”（换分类/回 latest）
            if random.random() < 0.25:
                latest_url = self.choose_latest_source()
                latest_json = self.get_latest_json(latest_url)
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
                if browse_ok:
                    logger.info("拟人浏览任务完成")
            except Exception as e:
                browse_ok = False
                logger.error(f"拟人浏览异常: {e}")

        self.send_notifications(browse_ok)

if __name__ == "__main__":
    if not USERNAME or not PASSWORD:
        raise SystemExit("Please set USERNAME/PASSWORD or LINUXDO_USERNAME/LINUXDO_PASSWORD")
    LinuxDoHumanBrowser().run()
