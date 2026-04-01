from __future__ import annotations

import base64
import json
import socket
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import requests
import yaml
from flask import Flask, jsonify, render_template, request


app = Flask(__name__)
CONFIG_FILE = Path("monitor_config.json")
SQLITE_FILE = Path("monitor.db")
CONFIG_KEYS = ["subscription_url", "interval_sec", "timeout_ms", "concurrency", "max_nodes"]


@dataclass
class Node:
    name: str
    host: str
    port: int
    protocol: str


class Database:
    def __init__(self) -> None:
        self.conn: sqlite3.Connection | None = None
        self.lock = threading.Lock()

    def _connect(self) -> sqlite3.Connection:
        if self.conn is not None:
            return self.conn

        self.conn = sqlite3.connect(str(SQLITE_FILE), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        return self.conn

    def init_schema(self) -> None:
        with self.lock:
            conn = self._connect()
            cur = conn.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS checks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    total INTEGER NOT NULL,
                    online INTEGER NOT NULL,
                    normal INTEGER NOT NULL,
                    slow INTEGER NOT NULL,
                    offline INTEGER NOT NULL,
                    avg_latency_ms REAL NULL,
                    error_msg TEXT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS node_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    check_id INTEGER NOT NULL,
                    node_name TEXT NOT NULL,
                    host TEXT NOT NULL,
                    port INTEGER NOT NULL,
                    protocol TEXT NOT NULL,
                    status TEXT NOT NULL,
                    latency_ms REAL NULL,
                    error_msg TEXT NULL,
                    success_rate REAL NOT NULL,
                    history_json TEXT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (check_id) REFERENCES checks(id) ON DELETE CASCADE
                )
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_node_results_check_id ON node_results(check_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_node_results_host_port ON node_results(host, port)")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS app_settings (
                    setting_key TEXT PRIMARY KEY,
                    setting_value TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.commit()

    def load_config(self) -> dict[str, Any]:
        with self.lock:
            conn = self._connect()
            cur = conn.cursor()
            cur.execute("SELECT setting_key, setting_value FROM app_settings")
            rows = cur.fetchall() or []
            result: dict[str, Any] = {}
            for row in rows:
                k = row["setting_key"]
                v = row["setting_value"]
                try:
                    result[k] = json.loads(v)
                except Exception:
                    result[k] = v
            return result

    def save_config(self, config: dict[str, Any]) -> None:
        with self.lock:
            conn = self._connect()
            cur = conn.cursor()
            values = [
                (
                    key,
                    json.dumps(config.get(key), ensure_ascii=False),
                    datetime.now().isoformat(sep=" ", timespec="seconds"),
                )
                for key in CONFIG_KEYS
            ]
            cur.executemany(
                """
                INSERT INTO app_settings (setting_key, setting_value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(setting_key) DO UPDATE SET
                    setting_value=excluded.setting_value,
                    updated_at=excluded.updated_at
                """,
                values,
            )
            conn.commit()

    def insert_check(self, started_ts: float, finished_ts: float, nodes: list[dict[str, Any]], error_msg: str | None) -> None:
        with self.lock:
            conn = self._connect()
            cur = conn.cursor()
            latency_values = [n["latency_ms"] for n in nodes if n.get("latency_ms") is not None]
            online = sum(1 for n in nodes if n["status"] == "online")
            normal = sum(1 for n in nodes if n["status"] == "normal")
            slow = sum(1 for n in nodes if n["status"] == "slow")
            offline = sum(1 for n in nodes if n["status"] == "offline")
            avg_latency = round(sum(latency_values) / len(latency_values), 2) if latency_values else None

            cur.execute(
                """
                INSERT INTO checks (
                    started_at, finished_at, total, online, normal, slow, offline, avg_latency_ms, error_msg
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.fromtimestamp(started_ts).isoformat(sep=" ", timespec="seconds"),
                    datetime.fromtimestamp(finished_ts).isoformat(sep=" ", timespec="seconds"),
                    len(nodes),
                    online,
                    normal,
                    slow,
                    offline,
                    avg_latency,
                    error_msg,
                ),
            )
            check_id = cur.lastrowid

            if nodes:
                values = [
                    (
                        check_id,
                        n["name"],
                        n["host"],
                        int(n["port"]),
                        n["protocol"],
                        n["status"],
                        n["latency_ms"],
                        n.get("error"),
                        n.get("success_rate", 0.0),
                        json.dumps(n.get("history", []), ensure_ascii=False),
                    )
                    for n in nodes
                ]
                cur.executemany(
                    """
                    INSERT INTO node_results (
                        check_id, node_name, host, port, protocol, status, latency_ms, error_msg, success_rate, history_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    values,
                )
            conn.commit()

    def load_latest_nodes(self) -> tuple[list[dict[str, Any]], str | None, float | None]:
        with self.lock:
            conn = self._connect()
            cur = conn.cursor()
            cur.execute("SELECT id, error_msg, finished_at FROM checks ORDER BY id DESC LIMIT 1")
            latest = cur.fetchone()
            if not latest:
                return [], None, None
            check_id = latest["id"]
            cur.execute(
                """
                SELECT node_name, host, port, protocol, status, latency_ms, error_msg, success_rate, history_json
                FROM node_results
                WHERE check_id = ?
                ORDER BY latency_ms IS NULL, latency_ms ASC
                """,
                (check_id,),
            )
            rows = cur.fetchall() or []
            nodes: list[dict[str, Any]] = []
            now_ts = time.time()
            for row in rows:
                hist = row["history_json"]
                history = []
                if isinstance(hist, str) and hist:
                    try:
                        parsed = json.loads(hist)
                        if isinstance(parsed, list):
                            history = normalize_history(parsed, fallback_ts=now_ts)
                    except Exception:
                        history = []
                history = prune_history(history, now_ts)
                nodes.append(
                    {
                        "name": row["node_name"],
                        "host": row["host"],
                        "port": int(row["port"]),
                        "protocol": row["protocol"],
                        "status": row["status"],
                        "latency_ms": float(row["latency_ms"]) if row["latency_ms"] is not None else None,
                        "error": row["error_msg"],
                        "success_rate": calc_success_rate(history),
                        "history": history,
                        "last_check": now_ts,
                    }
                )
            last_finished_at = None
            finished_at_text = latest["finished_at"]
            if isinstance(finished_at_text, str) and finished_at_text:
                try:
                    last_finished_at = datetime.fromisoformat(finished_at_text).timestamp()
                except Exception:
                    last_finished_at = None
            return nodes, latest["error_msg"], last_finished_at

    def load_nodes_from_recent_checks(self, within_sec: int) -> tuple[list[dict[str, Any]], str | None, float | None]:
        with self.lock:
            conn = self._connect()
            cur = conn.cursor()
            now_ts = time.time()
            cutoff_text = datetime.fromtimestamp(now_ts - within_sec).isoformat(sep=" ", timespec="seconds")
            cur.execute(
                """
                SELECT c.finished_at, c.error_msg, nr.node_name, nr.host, nr.port, nr.protocol, nr.status, nr.latency_ms, nr.error_msg AS node_error
                FROM checks c
                JOIN node_results nr ON nr.check_id = c.id
                WHERE c.finished_at >= ?
                ORDER BY c.finished_at ASC, c.id ASC
                """,
                (cutoff_text,),
            )
            rows = cur.fetchall() or []
            if not rows:
                cur.execute("SELECT id, error_msg, finished_at FROM checks ORDER BY id DESC LIMIT 1")
                latest = cur.fetchone()
                if not latest:
                    return [], None, None
                last_finished_at = None
                finished_at_text = latest["finished_at"]
                if isinstance(finished_at_text, str) and finished_at_text:
                    try:
                        last_finished_at = datetime.fromisoformat(finished_at_text).timestamp()
                    except Exception:
                        last_finished_at = None
                return [], latest["error_msg"], last_finished_at

            latest_error: str | None = None
            latest_ts: float | None = None
            nodes_map: dict[str, dict[str, Any]] = {}

            for row in rows:
                finished_at_text = row["finished_at"]
                try:
                    point_ts = datetime.fromisoformat(finished_at_text).timestamp()
                except Exception:
                    point_ts = now_ts

                latest_error = row["error_msg"] or latest_error
                latest_ts = point_ts if latest_ts is None or point_ts > latest_ts else latest_ts

                key = build_node_key(row["node_name"], row["host"], row["port"])
                node = nodes_map.get(key)
                if node is None:
                    node = {
                        "name": row["node_name"],
                        "host": row["host"],
                        "port": int(row["port"]),
                        "protocol": row["protocol"],
                        "status": row["status"],
                        "latency_ms": float(row["latency_ms"]) if row["latency_ms"] is not None else None,
                        "error": row["node_error"],
                        "history": [],
                        "success_rate": 0.0,
                        "last_check": point_ts,
                    }
                    nodes_map[key] = node

                node["status"] = row["status"]
                node["latency_ms"] = float(row["latency_ms"]) if row["latency_ms"] is not None else None
                node["error"] = row["node_error"]
                node["last_check"] = point_ts
                node["history"].append({"ts": point_ts, "status": row["status"]})

            nodes: list[dict[str, Any]] = []
            for n in nodes_map.values():
                history = prune_history(normalize_history(n.get("history", []), fallback_ts=now_ts), now_ts)
                n["history"] = history
                n["success_rate"] = calc_success_rate(history)
                nodes.append(n)

            nodes.sort(key=lambda x: ((x["latency_ms"] is None), x["latency_ms"] or 999999))
            return nodes, latest_error, latest_ts


db = Database()


class MonitorState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.last_check: float | None = None
        self.next_check: float | None = None
        self.last_error: str | None = None
        self.nodes: list[dict[str, Any]] = []
        self.history_retention_sec = 24 * 3600
        self.history_points_cap = 2000
        self.config: dict[str, Any] = {
            "subscription_url": "",
            "interval_sec": 60,
            "timeout_ms": 5000,
            "concurrency": 10,
            "max_nodes": 100,
        }


state = MonitorState()


def apply_config_values(values: dict[str, Any]) -> None:
    for key in CONFIG_KEYS:
        if key in values:
            state.config[key] = values[key]


def normalize_history(raw_history: Any, fallback_ts: float) -> list[dict[str, Any]]:
    if not isinstance(raw_history, list):
        return []
    normalized: list[dict[str, Any]] = []
    for item in raw_history:
        if isinstance(item, str):
            normalized.append({"ts": fallback_ts, "status": item})
            continue
        if not isinstance(item, dict):
            continue
        status = str(item.get("status", "")).strip()
        if not status:
            continue
        try:
            ts = float(item.get("ts"))
        except Exception:
            ts = fallback_ts
        normalized.append({"ts": ts, "status": status})
    return normalized


def prune_history(history: list[dict[str, Any]], now_ts: float) -> list[dict[str, Any]]:
    cutoff = now_ts - state.history_retention_sec
    kept = [x for x in history if float(x.get("ts", 0)) >= cutoff]
    return kept[-state.history_points_cap :]


def calc_success_rate(history: list[dict[str, Any]]) -> float:
    if not history:
        return 0.0
    ok_count = sum(1 for x in history if x.get("status") != "offline")
    return round(ok_count / len(history) * 100, 1)


def build_node_key(name: Any, host: Any, port: Any) -> str:
    return f"{name}|{host}|{port}"


def node_key_from_dict(node: dict[str, Any]) -> str:
    return build_node_key(node.get("name"), node.get("host"), node.get("port"))


def node_key_from_node(node: Node) -> str:
    return build_node_key(node.name, node.host, node.port)


def read_local_config() -> dict[str, Any]:
    if not CONFIG_FILE.exists():
        return {}
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: data[k] for k in CONFIG_KEYS if k in data}


def ts_to_str(ts: float | None) -> str:
    if ts is None:
        return "-"
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def safe_b64decode(data: str) -> bytes:
    text = data.strip()
    text += "=" * ((4 - len(text) % 4) % 4)
    return base64.urlsafe_b64decode(text.encode("utf-8"))


def parse_vmess(line: str) -> Node | None:
    raw = line[len("vmess://") :]
    try:
        obj = json.loads(safe_b64decode(raw).decode("utf-8", errors="ignore"))
        host = obj.get("add", "").strip()
        port = int(obj.get("port", 0))
        if not host or port <= 0:
            return None
        name = obj.get("ps") or f"{host}:{port}"
        return Node(name=name, host=host, port=port, protocol="vmess")
    except Exception:
        return None


def parse_uri(line: str, scheme: str) -> Node | None:
    try:
        u = urlsplit(line)
        host = u.hostname or ""
        port = u.port or 0
        if not host or port <= 0:
            return None
        name = unquote((u.fragment or "").strip()) or f"{host}:{port}"
        return Node(name=name, host=host, port=port, protocol=scheme)
    except Exception:
        return None


def parse_ss(line: str) -> Node | None:
    try:
        u = urlsplit(line)
        host = u.hostname or ""
        port = u.port or 0
        if host and port > 0:
            name = unquote((u.fragment or "").strip()) or f"{host}:{port}"
            return Node(name=name, host=host, port=port, protocol="ss")
    except Exception:
        pass

    try:
        body = line[len("ss://") :]
        left = body.split("#", 1)[0].split("?", 1)[0]
        decoded = safe_b64decode(left).decode("utf-8", errors="ignore")
        host_port = decoded.rsplit("@", 1)[-1]
        host, port_text = host_port.rsplit(":", 1)
        port = int(port_text)
        name = unquote(line.split("#", 1)[1]) if "#" in line else f"{host}:{port}"
        return Node(name=name, host=host, port=port, protocol="ss")
    except Exception:
        return None


def parse_clash_yaml(text: str) -> list[Node]:
    try:
        data = yaml.safe_load(text)
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    proxies = data.get("proxies", [])
    result: list[Node] = []
    if not isinstance(proxies, list):
        return result
    for p in proxies:
        if not isinstance(p, dict):
            continue
        host = str(p.get("server", "")).strip()
        port = p.get("port", 0)
        if not host:
            continue
        try:
            port = int(port)
        except Exception:
            continue
        if port <= 0:
            continue
        result.append(
            Node(
                name=str(p.get("name", f"{host}:{port}")),
                host=host,
                port=port,
                protocol=str(p.get("type", "proxy")),
            )
        )
    return result


def parse_subscription(content: str) -> list[Node]:
    stripped = content.strip()
    if not stripped:
        return []

    yaml_nodes = parse_clash_yaml(stripped)
    if yaml_nodes:
        return yaml_nodes

    if "\n" not in stripped:
        try:
            decoded = safe_b64decode(stripped).decode("utf-8", errors="ignore")
            if "\n" in decoded:
                stripped = decoded
        except Exception:
            pass

    nodes: list[Node] = []
    for raw in stripped.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("vmess://"):
            node = parse_vmess(line)
        elif line.startswith("vless://"):
            node = parse_uri(line, "vless")
        elif line.startswith("trojan://"):
            node = parse_uri(line, "trojan")
        elif line.startswith("ss://"):
            node = parse_ss(line)
        else:
            node = None
        if node is not None:
            nodes.append(node)
    return nodes


def tcp_ping(host: str, port: int, timeout_ms: int) -> tuple[bool, float | None, str | None]:
    start = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout_ms / 1000):
            elapsed = (time.perf_counter() - start) * 1000
            return True, elapsed, None
    except Exception as exc:
        return False, None, str(exc)


def classify(latency_ms: float | None, ok: bool) -> str:
    if not ok:
        return "offline"
    assert latency_ms is not None
    if latency_ms < 150:
        return "online"
    if latency_ms < 300:
        return "normal"
    return "slow"


def run_check_once(target_keys: set[str] | None = None) -> None:
    with state.lock:
        if state.running:
            return
        state.running = True
        state.last_error = None
        cfg = dict(state.config)

    started = time.time()
    try:
        sub_url = cfg.get("subscription_url", "").strip()
        if not sub_url:
            raise ValueError("subscription_url 未配置")

        response = requests.get(sub_url, timeout=15)
        response.raise_for_status()
        parsed = parse_subscription(response.text)
        if not parsed:
            raise ValueError("无法从订阅中解析节点")

        if target_keys:
            parsed = [x for x in parsed if node_key_from_node(x) in target_keys]
            if not parsed:
                raise ValueError("筛选节点在当前订阅中未匹配到可检测目标")
        else:
            parsed = parsed[: int(cfg.get("max_nodes", 100))]
        timeout_ms = int(cfg.get("timeout_ms", 5000))
        workers = max(1, int(cfg.get("concurrency", 10)))

        results: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(tcp_ping, node.host, node.port, timeout_ms): node for node in parsed
            }
            for fut in as_completed(futures):
                node = futures[fut]
                ok, latency, err = fut.result()
                now = time.time()
                results.append(
                    {
                        "name": node.name,
                        "host": node.host,
                        "port": node.port,
                        "protocol": node.protocol,
                        "status": classify(latency, ok),
                        "latency_ms": round(latency, 2) if latency is not None else None,
                        "error": err,
                        "last_check": now,
                    }
                )

        now = time.time()
        old_nodes = list(state.nodes)
        node_map = {(n["name"], n["host"], n["port"]): n for n in old_nodes}
        detected_nodes: list[dict[str, Any]] = []
        for result in results:
            key = (result["name"], result["host"], result["port"])
            old = node_map.get(key)
            history = []
            if old and isinstance(old.get("history"), list):
                history = normalize_history(old["history"], fallback_ts=now)
            history = prune_history(history, now)
            history.append({"ts": now, "status": result["status"]})
            history = prune_history(history, now)
            result["history"] = history
            result["success_rate"] = calc_success_rate(history)
            detected_nodes.append(result)

        if target_keys:
            merged_map = {node_key_from_dict(x): x for x in old_nodes}
            for item in detected_nodes:
                merged_map[node_key_from_dict(item)] = item
            new_nodes = list(merged_map.values())
        else:
            new_nodes = detected_nodes
        new_nodes.sort(key=lambda x: ((x["latency_ms"] is None), x["latency_ms"] or 999999))

        with state.lock:
            state.nodes = new_nodes
            state.last_check = now
            state.next_check = now + int(cfg.get("interval_sec", 60))
            state.running = False

        try:
            db.insert_check(started, now, detected_nodes if target_keys else new_nodes, None)
        except Exception as db_exc:
            with state.lock:
                state.last_error = f"SQLite 入库失败: {db_exc}"
    except Exception as exc:
        finished = time.time()
        error_text = str(exc)
        with state.lock:
            prev_nodes = list(state.nodes)
        prev_map = {node_key_from_dict(x): x for x in prev_nodes}
        affected_nodes = prev_nodes
        if target_keys:
            affected_nodes = [prev_map[k] for k in target_keys if k in prev_map]
        offline_nodes: list[dict[str, Any]] = []
        for old in affected_nodes:
            history = normalize_history(old.get("history", []), fallback_ts=finished)
            history = prune_history(history, finished)
            history.append({"ts": finished, "status": "offline"})
            history = prune_history(history, finished)
            offline_nodes.append(
                {
                    "name": old.get("name"),
                    "host": old.get("host"),
                    "port": old.get("port"),
                    "protocol": old.get("protocol"),
                    "status": "offline",
                    "latency_ms": None,
                    "error": error_text,
                    "last_check": finished,
                    "history": history,
                    "success_rate": calc_success_rate(history),
                }
            )
        with state.lock:
            if target_keys:
                merged_map = {node_key_from_dict(x): x for x in prev_nodes}
                for item in offline_nodes:
                    merged_map[node_key_from_dict(item)] = item
                state.nodes = list(merged_map.values())
            else:
                state.nodes = offline_nodes
            state.last_error = error_text
            state.last_check = finished
            state.next_check = finished + int(cfg.get("interval_sec", 60))
            state.running = False
        try:
            db.insert_check(started, finished, offline_nodes, error_text)
        except Exception:
            pass


def scheduler_loop() -> None:
    while True:
        with state.lock:
            interval = int(state.config.get("interval_sec", 60))
            now = time.time()
            if state.next_check is None:
                if state.last_check is None:
                    state.next_check = now
                else:
                    state.next_check = state.last_check + interval
            should_run = not state.running and state.next_check is not None and now >= state.next_check
            if should_run:
                state.next_check = now + interval
        if should_run:
            threading.Thread(target=run_check_once, daemon=True).start()
        time.sleep(1)


@app.route("/")
def index() -> str:
    return render_template("index.html")


@app.get("/api/status")
def api_status() -> Any:
    with state.lock:
        nodes = list(state.nodes)
        cfg = dict(state.config)
        running = state.running
        last_check = state.last_check
        next_check = state.next_check
        last_error = state.last_error

    total = len(nodes)
    online = sum(1 for n in nodes if n["status"] == "online")
    normal = sum(1 for n in nodes if n["status"] == "normal")
    slow = sum(1 for n in nodes if n["status"] == "slow")
    offline = sum(1 for n in nodes if n["status"] == "offline")
    latency_values = [n["latency_ms"] for n in nodes if n["latency_ms"] is not None]
    avg_latency = round(sum(latency_values) / len(latency_values), 2) if latency_values else None

    return jsonify(
        {
            "running": running,
            "last_check": ts_to_str(last_check),
            "next_check": ts_to_str(next_check),
            "last_error": last_error,
            "config": cfg,
            "summary": {
                "total": total,
                "online": online,
                "normal": normal,
                "slow": slow,
                "offline": offline,
                "avg_latency_ms": avg_latency,
            },
            "nodes": nodes,
        }
    )


@app.post("/api/config")
def api_config() -> Any:
    data = request.get_json(force=True)
    with state.lock:
        for key in CONFIG_KEYS:
            if key in data:
                state.config[key] = data[key]
        cfg_snapshot = dict(state.config)
    try:
        db.save_config(cfg_snapshot)
    except Exception as exc:
        with state.lock:
            state.last_error = f"配置写入数据库失败: {exc}"
    return jsonify({"ok": True})


@app.post("/api/run-once")
def api_run_once() -> Any:
    data = request.get_json(silent=True) or {}
    raw_keys = data.get("target_keys")
    target_keys: set[str] | None = None
    if isinstance(raw_keys, list):
        cleaned = {str(x).strip() for x in raw_keys if str(x).strip()}
        if cleaned:
            target_keys = cleaned
    threading.Thread(target=run_check_once, args=(target_keys,), daemon=True).start()
    return jsonify({"ok": True})


def bootstrap() -> None:
    try:
        db.init_schema()
        db_config = db.load_config()
        if db_config:
            with state.lock:
                apply_config_values(db_config)
        else:
            # 兼容旧版本：首次启动时迁移本地 monitor_config.json 到数据库
            local_cfg = read_local_config()
            if local_cfg:
                with state.lock:
                    apply_config_values(local_cfg)
                    cfg_snapshot = dict(state.config)
                db.save_config(cfg_snapshot)
        nodes, db_error, last_finished_at = db.load_nodes_from_recent_checks(within_sec=24 * 3600)
        with state.lock:
            if nodes:
                state.nodes = nodes
            if last_finished_at is not None:
                state.last_check = last_finished_at
                state.next_check = last_finished_at + int(state.config.get("interval_sec", 60))
            if db_error:
                state.last_error = db_error
    except Exception as exc:
        with state.lock:
            state.last_error = f"SQLite 初始化失败: {exc}"


def main() -> None:
    bootstrap()
    t = threading.Thread(target=scheduler_loop, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=3000, debug=False)


if __name__ == "__main__":
    main()
