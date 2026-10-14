"""Local UI QA fixture server. All mutations stay in memory; no Gmail or database access.

Run from the repository root after npm --prefix frontend run build:
    python frontend/qa/preview_server.py
"""
import json
from datetime import datetime, timedelta, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

DIST = Path(__file__).resolve().parents[1] / "dist"
DAY = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
LISTS = [
    {"id": 1, "name": "Inbox", "color": "#70757d"},
    {"id": 2, "name": "D003 · 异步与 HTTP 客户端：配置、并发控制与测试", "color": "#da8b24"},
    {"id": 3, "name": "第 1 周 | 学习日 1/156（基线诊断与工程骨架）", "color": "#70757d"},
    {"id": 4, "name": "待办事项", "color": "#2f9e61"},
] + [{"id": n, "name": f"学习清单 {n - 4:02} · 实践与复盘", "color": "#3984d8"} for n in range(5, 23)]


def item(id, title, list_id=2, completed=False, parent_id=None):
    return dict(id=id, title=title, list_id=list_id, completed=completed, parent_id=parent_id,
                description="阅读资料并完成实现，记录测试结果与下一步。", position=id,
                due_date=DAY, tags=[], metadata={}, estimated_minutes=30,
                created_at=DAY + "T09:00:00Z", updated_at=DAY + "T09:00:00Z")


ITEMS = [item(1, "复测 LC438：滑动窗口统计字符频次"),
         item(2, "阅读 asyncio 的 Task、gather 和 Semaphore 用法"),
         item(3, "实现异步 HTTP 请求客户端：配置超时，最多并发 3 个请求"),
         item(4, "用模拟服务测试正常响应、超时与并发上限"),
         item(5, "提交客户端与测试结果"),
         item(6, "编写正常响应测试", parent_id=4),
         item(7, "验证超时与重试", parent_id=4),
         item(8, "完成资料精读与结论提炼", 3, True),
         item(9, "完成能力自评与岗位画像交付", 3, True)]


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DIST), **kwargs)

    def respond(self, value, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(value, ensure_ascii=False).encode())

    def do_GET(self):
        path = urlsplit(self.path).path
        if not path.startswith('/api/'):
            return super().do_GET()
        if path == '/api/lists':
            return self.respond([dict(l, position=i, item_count=sum(t['list_id']==l['id'] and not t['parent_id'] for t in ITEMS), pending_count=sum(t['list_id']==l['id'] and not t['completed'] and not t['parent_id'] for t in ITEMS)) for i,l in enumerate(LISTS)])
        if path == '/api/items':
            query = parse_qs(urlsplit(self.path).query)
            offset = int(query.get('offset', ['0'])[0])
            return self.respond(ITEMS[offset:offset+1000])
        responses = {
            '/api/plans': [dict(id=1, title="Python 异步学习计划", content_type="learning", status="confirmed", created_at=DAY+"T09:00:00Z")],
            '/api/settings/ui': dict(sidebar_width=256),
            '/api/workspace/pending-count': dict(count=1),
            '/api/workspace/task-folds': dict(folds={}),
            '/api/integrations/gmail/status': dict(client_configured=True, connected=True, email="demo@example.com"),
            '/api/settings/providers': [dict(id=1, name="示例模型服务", base_url="https://api.example.com/v1", model="example-model", api_key_env="", has_api_key=True, enabled=True)],
            '/api/workspace/gmail-job': dict(status='idle', stage='idle', processed=0, total=0),
            '/api/workspace/usage': dict(calls=12, input_tokens=18000, output_tokens=2400, daily_call_limit=200, recent=[]),
            '/api/workspace/ai-controls': dict(daily_call_limit=200, sender_allowlist=[], sender_blocklist=[]),
            '/api/workspace/pending': [dict(id=1, subject="本周项目交付安排", preview_title="准备项目评审资料", sender="team@example.com", status="candidate", reason="邮件提到了需要确认的交付任务。", attempt_count=1, received_at=DAY+"T09:00:00Z", gmail_url="#")],
            '/api/workspace/daily': dict(capacity_minutes=240, estimated_minutes=60, over_capacity=False, planned_ids=[1,2], overdue_ids=[], completed_ids=[]),
            '/api/workspace/review': dict(completed=[dict(id=8, title="完成资料精读与结论提炼", evidence=[], result="资料与结论已整理。")], unfinished=[dict(id=1,title="复测 LC438", reason="等待测试结果")], markdown=""),
            '/api/workspace/trash': [],
            '/api/workspace/actions': [dict(id=1,summary="完成学习任务",kind="complete",undone=False,created_at=DAY+"T09:00:00Z")],
        }
        return self.respond(responses.get(path, {}))

    def do_POST(self):
        path = urlsplit(self.path).path
        if path.startswith('/api/items/') and path.endswith('/complete'):
            target = next(t for t in ITEMS if t['id']==int(path.split('/')[3]))
            target['completed'] = not target['completed']
            return self.respond(target)
        return self.respond({})

    def do_PUT(self):
        return self.respond({})


if __name__ == '__main__':
    print('UI QA preview: http://127.0.0.1:4178 (in-memory example data only)', flush=True)
    ThreadingHTTPServer(('127.0.0.1', 4178), Handler).serve_forever()
