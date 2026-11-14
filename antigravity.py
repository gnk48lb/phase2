"""Drive the Antigravity desktop app through its own CDP debug port.

This is the sanctioned path: the app's agent runs the task exactly as it would
for a human user.  No Google API is called directly from here, so the account
is not exposed to automated traffic.

    python antigravity.py probe                       # read-only DOM summary
    python antigravity.py say "run this command ..."   # type into the agent chat
    python antigravity.py shot                        # save a screenshot
"""
from __future__ import annotations

import argparse
import base64
import json
import time
from pathlib import Path
from typing import Any

import requests
import websocket

CDP_PORT = 4794
ROOT = Path(__file__).resolve().parent


def page_target() -> dict[str, Any]:
    targets = requests.get(f"http://127.0.0.1:{CDP_PORT}/json/list", timeout=10).json()
    for target in targets:
        if target.get("type") == "page" and "antigravity" in (target.get("url") or "").lower():
            return target
    for target in targets:
        if target.get("type") == "page":
            return target
    raise RuntimeError("no Antigravity page target found")


class CDP:
    def __init__(self) -> None:
        self.target = page_target()
        try:
            self.ws = websocket.create_connection(self.target["webSocketDebuggerUrl"],
                                                  timeout=30, suppress_origin=True)
        except Exception:
            self.ws = websocket.create_connection(self.target["webSocketDebuggerUrl"],
                                                  timeout=30,
                                                  origin="http://127.0.0.1:4794")
        self.counter = 0

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.counter += 1
        self.ws.send(json.dumps({"id": self.counter, "method": method,
                                 "params": params or {}}))
        while True:
            message = json.loads(self.ws.recv())
            if message.get("id") == self.counter:
                if "error" in message:
                    raise RuntimeError(f"{method}: {message['error']}")
                return message.get("result", {})

    def evaluate(self, expression: str, await_promise: bool = False) -> Any:
        result = self.call("Runtime.evaluate", {
            "expression": expression, "returnByValue": True,
            "awaitPromise": await_promise, "userGesture": True})
        if result.get("exceptionDetails"):
            return {"__error": str(result["exceptionDetails"])[:400]}
        return result.get("result", {}).get("value")

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass


PROBE_JS = r"""
(() => {
  const inputs = [...document.querySelectorAll('textarea, input, [contenteditable="true"]')]
    .map(e => ({tag: e.tagName, ph: e.getAttribute('placeholder') || e.getAttribute('aria-label') || '',
                 cls: (e.className || '').toString().slice(0, 60)}));
  const buttons = [...document.querySelectorAll('button')]
    .map(b => (b.getAttribute('aria-label') || b.title || b.innerText || '').trim().slice(0, 40))
    .filter(Boolean).slice(0, 40);
  return {url: location.href, title: document.title,
          text: document.body.innerText.slice(0, 1500),
          inputs, buttons};
})()
"""


def cmd_probe(cdp: CDP) -> None:
    print(json.dumps(cdp.evaluate(PROBE_JS), ensure_ascii=False, indent=1))


def cmd_shot(cdp: CDP, path: Path) -> None:
    result = cdp.call("Page.captureScreenshot", {"format": "png"})
    path.write_bytes(base64.b64decode(result["data"]))
    print(f"saved {path}")


def cmd_say(cdp: CDP, text: str, submit: bool) -> None:
    payload = json.dumps(text)
    script = f"""
    (() => {{
      const text = {payload};
      const el = [...document.querySelectorAll('textarea, [contenteditable="true"]')]
        .filter(e => e.offsetParent !== null)
        .pop();
      if (!el) return 'no input found';
      const setter = Object.getOwnPropertyDescriptor(
        el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLElement.prototype, 'value');
      if (setter && setter.set) setter.set.call(el, text);
      el.dispatchEvent(new Event('input', {{bubbles: true}}));
      el.dispatchEvent(new Event('change', {{bubbles: true}}));
      if (el.focus) el.focus();
      return 'typed';
    }})()
    """
    print(cdp.evaluate(script))
    if not submit:
        return
    time.sleep(0.5)
    submit_js = """
    (() => {
      const el = [...document.querySelectorAll('textarea, [contenteditable="true"]')]
        .filter(e => e.offsetParent !== null).pop();
      if (!el) return 'no input';
      el.focus();
      const btns = [...document.querySelectorAll('button')].filter(b => b.offsetParent !== null);
      const send = btns.find(b => /send|submit|发送/i.test(
        (b.getAttribute('aria-label') || '') + ' ' + (b.title || '')));
      if (send) { send.click(); return 'clicked send'; }
      const ev = new KeyboardEvent('keydown', {key: 'Enter', code: 'Enter', keyCode: 13,
                                               which: 13, bubbles: true});
      el.dispatchEvent(ev);
      return 'pressed enter';
    })()
    """
    print(cdp.evaluate(submit_js))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("probe", "say", "shot"))
    parser.add_argument("text", nargs="*")
    parser.add_argument("--no-submit", action="store_true")
    parser.add_argument("--out", type=Path, default=ROOT / "antigravity_shot.png")
    args = parser.parse_args()
    cdp = CDP()
    try:
        if args.command == "probe":
            cmd_probe(cdp)
        elif args.command == "say":
            cmd_say(cdp, " ".join(args.text), submit=not args.no_submit)
        else:
            cmd_shot(cdp, args.out)
    finally:
        cdp.close()


if __name__ == "__main__":
    main()
