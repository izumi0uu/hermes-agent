"""Compatibility regression tests for Photon sidecar spectrum-ts drift.

These exercise the real Node sidecar against a tiny fake ``spectrum-ts``
package that intentionally omits the v3-only ``markdown()`` / ``typing()``
builders. The sidecar must keep outbound delivery working (markdown falls back
to ``text()``) and skip typing indicators without turning either path into a
500.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import textwrap
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pytest


NODE_BIN = shutil.which("node")


_PATCHABLE_SPECTRUM_CHUNK = textwrap.dedent(
    """
    var rebuildFromAppleMessage = async (client, message, phone, chatGuidHint) => {
      const messageGuidStr = message.guid;
      const timestamp = message.dateCreated ?? /* @__PURE__ */ new Date();
      const base = buildMessageBase(message, chatGuidHint, timestamp, phone);
      const attachments = messageAttachments(message);
      if (attachments.length === 1) {
        const info = attachments[0];
        if (!info) {
          throw new Error("Unreachable: attachments.length === 1 but no element");
        }
        return buildAttachmentMessage(client, base, info, messageGuidStr, 0);
      }
      if (attachments.length > 1) {
        const items = [];
        for (let i = 0; i < attachments.length; i++) {
          const info = attachments[i];
          if (!info) {
            continue;
          }
          items.push(
            await buildAttachmentMessage(
              client,
              base,
              info,
              formatChildId(i, messageGuidStr),
              i,
              messageGuidStr
            )
          );
        }
        return {
          ...base,
          id: messageGuidStr,
          content: asProviderGroup(items)
        };
      }
      if (getBalloonBundleId(message) === URL_BALLOON_BUNDLE_ID) {
        return toRichlinkMessage(message, base, messageGuidStr);
      }
      const text2 = message.content.text;
      return {
        ...base,
        id: messageGuidStr,
        content: text2 ? asText(text2) : asCustom(message)
      };
    };
    var toInboundMessages = async (client, cache, event, phone) => {
      const base = buildMessageBase(
        event.message,
        event.chatGuid,
        event.occurredAt,
        phone
      );
      const messageGuidStr = event.message.guid;
      if (getBalloonBundleId(event.message) === URL_BALLOON_BUNDLE_ID) {
        const msg2 = toRichlinkMessage(event.message, base, messageGuidStr);
        cacheMessage(cache, msg2);
        return [msg2];
      }
      const attachments = messageAttachments(event.message);
      if (attachments.length === 1) {
        const info = attachments[0];
        if (!info) {
          throw new Error("Unreachable: attachments.length === 1 but no element");
        }
        const msg2 = await buildAttachmentMessage(
          client,
          base,
          info,
          messageGuidStr,
          0
        );
        cacheMessage(cache, msg2);
        return [msg2];
      }
      if (attachments.length > 1) {
        const items = [];
        for (let i = 0; i < attachments.length; i++) {
          const info = attachments[i];
          if (!info) {
            continue;
          }
          items.push(
            await buildAttachmentMessage(
              client,
              base,
              info,
              formatChildId(i, messageGuidStr),
              i,
              messageGuidStr
            )
          );
        }
        const parent = {
          ...base,
          id: messageGuidStr,
          content: asProviderGroup(items)
        };
        cacheMessage(cache, parent);
        return [parent];
      }
      const text2 = event.message.content.text;
      const msg = {
        ...base,
        id: messageGuidStr,
        content: text2 ? asText(text2) : asCustom(event.message)
      };
      cacheMessage(cache, msg);
      return [msg];
    };
    """
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _post_json(base_url: str, token: str, path: str, body: dict) -> dict:
    req = Request(
        f"{base_url}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Hermes-Sidecar-Token": token,
        },
        method="POST",
    )
    with urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _write_fake_spectrum(sidecar_dir: Path) -> None:
    spectrum_dir = sidecar_dir / "node_modules" / "spectrum-ts"
    providers_dir = spectrum_dir / "providers"
    dist_dir = spectrum_dir / "dist"
    providers_dir.mkdir(parents=True)
    dist_dir.mkdir(parents=True)

    (spectrum_dir / "package.json").write_text(
        json.dumps(
            {
                "name": "spectrum-ts",
                "version": "2.0.0",
                "type": "module",
                "exports": {
                    ".": "./index.js",
                    "./providers/imessage": "./providers/imessage.js",
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (spectrum_dir / "index.js").write_text(
        textwrap.dedent(
            """
            export async function Spectrum() {
              return {
                messages: (async function* () {
                  await new Promise(() => {});
                })(),
                async stop() {},
              };
            }

            export const text = (value) => ({ kind: "text", value });
            export const attachment = (value, options) => ({ kind: "attachment", value, options });
            export const voice = (value, options) => ({ kind: "voice", value, options });
            """
        ),
        encoding="utf-8",
    )
    (providers_dir / "imessage.js").write_text(
        textwrap.dedent(
            """
            function makeReactionHandle() {
              return {
                id: "reaction-1",
                async unsend() {},
              };
            }

            function makeMessage(id) {
              return {
                id,
                async react() {
                  return makeReactionHandle();
                },
              };
            }

            function makeSpace(id) {
              return {
                id,
                async send(builder) {
                  return { id: `${builder.kind || "message"}-1` };
                },
                async getMessage(id) {
                  return makeMessage(id);
                },
                async unsend() {},
              };
            }

            export function imessage(app) {
              return {
                space: {
                  async create(id) {
                    return makeSpace(id);
                  },
                  async get(id) {
                    return makeSpace(id);
                  },
                },
              };
            }

            imessage.config = () => ({ provider: "imessage" });
            """
        ),
        encoding="utf-8",
    )
    (dist_dir / "chunk-test.js").write_text(
        _PATCHABLE_SPECTRUM_CHUNK,
        encoding="utf-8",
    )


def _copy_sidecar_sources(tmp_path: Path) -> Path:
    src_dir = Path("plugins/platforms/photon/sidecar")
    sidecar_dir = tmp_path / "sidecar"
    sidecar_dir.mkdir()
    shutil.copy2(src_dir / "index.mjs", sidecar_dir / "index.mjs")
    shutil.copy2(
        src_dir / "patch-spectrum-mixed-attachments.mjs",
        sidecar_dir / "patch-spectrum-mixed-attachments.mjs",
    )
    _write_fake_spectrum(sidecar_dir)
    return sidecar_dir


@pytest.mark.skipif(NODE_BIN is None, reason="node is required for Photon sidecar tests")
def test_sidecar_degrades_builder_drift_without_dropping_replies(tmp_path: Path) -> None:
    sidecar_dir = _copy_sidecar_sources(tmp_path)
    port = _free_port()
    token = "test-sidecar-token"
    proc = subprocess.Popen(
        [NODE_BIN, "index.mjs"],
        cwd=sidecar_dir,
        env={
            **os.environ,
            "PHOTON_PROJECT_ID": "proj-test",
            "PHOTON_PROJECT_SECRET": "secret-test",
            "PHOTON_SIDECAR_PORT": str(port),
            "PHOTON_SIDECAR_TOKEN": token,
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    base_url = f"http://127.0.0.1:{port}"

    try:
        deadline = time.time() + 10
        while True:
            try:
                health = _post_json(base_url, token, "/healthz", {})
                if health.get("ok") is True:
                    break
            except (HTTPError, URLError, OSError):
                if time.time() >= deadline:
                    raise
                time.sleep(0.1)

        sent = _post_json(
            base_url,
            token,
            "/send",
            {
                "spaceId": "+15551234567",
                "text": "**hello**",
                "format": "markdown",
            },
        )
        typing = _post_json(
            base_url,
            token,
            "/typing",
            {"spaceId": "+15551234567", "state": "start"},
        )

        assert sent["ok"] is True
        assert sent["messageId"] == "text-1"
        assert typing == {"ok": True, "skipped": True}
    finally:
        try:
            _post_json(base_url, token, "/shutdown", {})
        except Exception:
            proc.terminate()
        proc.wait(timeout=10)

    stderr = (proc.stderr.read() if proc.stderr else "") or ""
    assert "loaded spectrum-ts@2.0.0" in stderr
    assert "missing markdown()" in stderr
    assert "missing typing()" in stderr
