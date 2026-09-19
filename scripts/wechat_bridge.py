#!/usr/bin/env python3
"""Codex standalone WeChat/iLink bridge (generic, shareable).

This version contains NO real credentials. Everything (WeChat iLink token/account
and the LLM provider token/model) is read from a JSON config file at runtime.

Usage:
  python wechat_bridge.py login
  python wechat_bridge.py poll
  python wechat_bridge.py run
  python wechat_bridge.py send "text"

Config:
  Copy config.example.json to config.json, fill in real values, and pass it with
  --config (or set WECHAT_CONFIG to the path). Never commit config.json.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import random
import sys
import time
import uuid
from pathlib import Path

import requests


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config.json"
STATE_DIR = ROOT / "state"

ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
ILINK_APP_ID = "bot"
CHANNEL_VERSION = "2.2.0"
ILINK_APP_CLIENT_VERSION = str((2 << 16) | (2 << 8) | 0)
EP_GET_UPDATES = "ilink/bot/getupdates"
EP_SEND_MESSAGE = "ilink/bot/sendmessage"
EP_GET_BOT_QR = "ilink/bot/get_bot_qrcode"
EP_GET_QR_STATUS = "ilink/bot/get_qrcode_status"
LONG_POLL_TIMEOUT = 35
QR_TIMEOUT = 480


def load_config() -> dict:
    path = Path(os.environ.get("WECHAT_CONFIG", DEFAULT_CONFIG))
    if not path.exists():
        sys.exit(f"缺少配置：{path}。请复制 config.example.json 为 config.json 并填入真实值。")
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _headers(token: str, body: str) -> dict:
    uin = base64.b64encode(str(random.SystemRandom().randint(0, 2**31)).encode("utf-8")).decode("ascii")
    return {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": uin,
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": ILINK_APP_CLIENT_VERSION,
        "Authorization": f"Bearer {token}",
    }


def _post(cfg: dict, endpoint: str, payload: dict, timeout: int = 40) -> dict:
    body = json.dumps(
        {**payload, "base_info": {"channel_version": CHANNEL_VERSION}},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    url = f"{cfg['base_url'].rstrip('/')}/{endpoint}"
    resp = requests.post(url, headers=_headers(cfg["token"], body), data=body, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _api_get_no_auth(endpoint: str, params: dict, timeout: int = 35) -> dict:
    headers = {
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": ILINK_APP_CLIENT_VERSION,
    }
    url = f"{ILINK_BASE_URL.rstrip('/')}/{endpoint}"
    resp = requests.get(url, headers=headers, params=params, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _state(key: str, default):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = STATE_DIR / f"{key}.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return default


def _save_state(key: str, data) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / f"{key}.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _extract_text(msg: dict) -> str:
    for item in msg.get("item_list") or []:
        if item.get("type") == 1:
            return str((item.get("text_item") or {}).get("text") or "").strip()
    return ""


def send_text(cfg: dict, to: str, text: str, context_token: str | None = None) -> dict:
    msg = {
        "from_user_id": "",
        "to_user_id": to,
        "client_id": "codex-wechat-" + uuid.uuid4().hex,
        "message_type": 2,
        "message_state": 2,
        "item_list": [{"type": 1, "text_item": {"text": text}}],
    }
    if context_token:
        msg["context_token"] = context_token
    return _post(cfg, EP_SEND_MESSAGE, {"msg": msg}, timeout=30)


def fetch_updates(cfg: dict) -> list:
    sync = _state("sync", {})
    buf = sync.get("get_updates_buf", "")
    resp = _post(cfg, EP_GET_UPDATES, {"get_updates_buf": buf}, timeout=LONG_POLL_TIMEOUT + 10)
    new_buf = resp.get("get_updates_buf") or buf
    _save_state("sync", {"get_updates_buf": new_buf})
    return resp.get("msgs") or []


def _extract_final_answer(raw: str) -> str:
    if not raw:
        return ""
    marker = "tokens used"
    idx = raw.rfind(marker)
    if idx != -1:
        tail = raw[idx + len(marker):].strip()
        lines = [ln for ln in tail.splitlines() if ln.strip()]
        if lines and lines[0].strip().replace(",", "").replace(".", "").isdigit():
            lines = lines[1:]
        if lines:
            return "\n".join(lines).strip()
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    return lines[-1].strip() if lines else ""


def llm_reply(cfg: dict, text: str) -> str:
    """Reply by calling the LLM provider directly. No CLI process needed."""
    llm_base = cfg.get("llm_base_url")
    llm_token = cfg.get("llm_token")
    llm_model = cfg.get("llm_model") or "deepseek-v4-flash"
    instructions = ""
    inst_path = Path(cfg.get("instructions_file", "") or "")
    if inst_path.exists():
        instructions = inst_path.read_text(encoding="utf-8")
    if llm_base and llm_token:
        try:
            payload = {"model": llm_model, "input": text}
            if instructions:
                payload["instructions"] = instructions
            body = json.dumps(payload, ensure_ascii=False)
            resp = requests.post(
                f"{llm_base.rstrip('/')}/responses",
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {llm_token}"},
                data=body,
                timeout=60,
            )
            resp.raise_for_status()
            data = resp.json()
            for item in data.get("output") or []:
                if item.get("type") != "message":
                    continue
                for c in item.get("content") or []:
                    if c.get("type") == "output_text" and c.get("text"):
                        return c["text"].strip()
        except Exception as exc:
            return f"（模型调用失败：{exc}）"
    return "（未配置 LLM，无法回复）"


def qr_login() -> None:
    """Request QR, wait for scan, save new credentials to config."""
    cfg = load_config()
    print("正在向 iLink 请求二维码...")
    qr_resp = _api_get_no_auth(EP_GET_BOT_QR, {"bot_type": "3"}, timeout=35)
    qrcode = str(qr_resp.get("qrcode") or "")
    qr_img = str(qr_resp.get("qrcode_img_content") or "")
    if not qrcode:
        sys.exit("二维码请求失败，未拿到 qrcode")

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / "login_qr.txt").write_text(qr_img or qrcode, encoding="utf-8")
    print("请用微信扫一扫扫描二维码，或在电脑浏览器打开：")
    print(qr_img or qrcode)
    print("二维码链接已保存到", STATE_DIR / "login_qr.txt")

    deadline = time.monotonic() + QR_TIMEOUT
    while time.monotonic() < deadline:
        try:
            status_resp = _api_get_no_auth(EP_GET_QR_STATUS, {"qrcode": qrcode}, timeout=35)
        except Exception as exc:
            print("状态轮询异常:", exc)
            time.sleep(2)
            continue
        status = str(status_resp.get("status") or "wait")
        if status == "wait":
            print(".", end="", flush=True)
        elif status == "scaned":
            print("\n已扫码，请在微信里确认登录...")
        elif status == "expired":
            print("\n二维码已过期，请重新运行登录。")
            sys.exit(1)
        elif status == "confirmed":
            account_id = str(status_resp.get("ilink_bot_id") or "")
            token = str(status_resp.get("bot_token") or "")
            base_url = str(status_resp.get("baseurl") or ILINK_BASE_URL)
            user_id = str(status_resp.get("ilink_user_id") or "")
            if not account_id or not token:
                sys.exit("确认失败：凭据不完整")
            cfg["account_id"] = account_id
            cfg["token"] = token
            cfg["base_url"] = base_url
            cfg["user_id"] = user_id
            cfg["home_channel"] = user_id
            path = Path(os.environ.get("WECHAT_CONFIG", DEFAULT_CONFIG))
            path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"\n微信连接成功，account_id={account_id}")
            print("凭据已保存到", path)
            return
        time.sleep(1)
    print("\n扫码登录超时。")
    sys.exit(1)


def poll_once(cfg: dict) -> None:
    msgs = fetch_updates(cfg)
    for m in msgs:
        text = _extract_text(m)
        sender = m.get("from_user_id") or ""
        ctx = m.get("context_token") or ""
        print(json.dumps({"from": sender, "text": text, "has_context": bool(ctx)}, ensure_ascii=False))
    if not msgs:
        print("no new messages")


def run_loop(cfg: dict) -> None:
    print("wechat bridge running...")
    while True:
        try:
            msgs = fetch_updates(cfg)
        except Exception as exc:
            print("poll error:", exc)
            time.sleep(5)
            continue
        for m in msgs:
            text = _extract_text(m)
            sender = m.get("from_user_id") or ""
            ctx = m.get("context_token") or ""
            if not text or not sender:
                continue
            if ctx:
                ctxs = _state("context_tokens", {})
                ctxs[sender] = ctx
                _save_state("context_tokens", ctxs)
            print(f"[recv] {sender}: {text}")
            use_ctx = ctx or _state("context_tokens", {}).get(sender)
            reply = llm_reply(cfg, text)
            try:
                r = send_text(cfg, sender, reply, use_ctx)
                print("[sent]", r)
            except Exception as exc:
                print("[send error]", exc)


def main() -> None:
    parser = argparse.ArgumentParser(description="Codex WeChat/iLink bridge")
    parser.add_argument("--config", default=None, help="Path to config.json")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("poll")
    sub.add_parser("run")
    sub.add_parser("login")
    p_send = sub.add_parser("send")
    p_send.add_argument("text")
    args = parser.parse_args()
    if args.config:
        os.environ["WECHAT_CONFIG"] = args.config
    cfg = load_config()

    if args.cmd == "poll":
        poll_once(cfg)
    elif args.cmd == "run":
        run_loop(cfg)
    elif args.cmd == "login":
        qr_login()
    elif args.cmd == "send":
        ctx = _state("context_tokens", {}).get(cfg["home_channel"])
        r = send_text(cfg, cfg["home_channel"], args.text, ctx)
        print(json.dumps(r, ensure_ascii=False))


if __name__ == "__main__":
    main()
