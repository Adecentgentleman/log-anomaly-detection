# -*- coding: utf-8 -*-
"""回答生成模块：默认离线模板拼装，配置了 API key 才走 LLM。

两种模式怎么切换
----------------
* **离线检索模式（默认）**：不需要任何 API key、不联网。取检索 top1 的 answer 作为回答主体，
  用 prompts/answer_template.md 拼装，末尾附「参考」列出 top 文档标题与相似度。
* **LLM 生成模式**：设置环境变量后自动启用——

      # Linux / macOS
      export LLM_API_KEY="sk-xxxx"
      export LLM_BASE_URL="https://api.openai.com/v1"   # 可选，默认就是它
      export LLM_MODEL="gpt-4o-mini"                    # 可选，默认就是它

      # Windows PowerShell
      $env:LLM_API_KEY="sk-xxxx"

  然后照常运行 `python src/main.py --query "..."` 即可。走的是 OpenAI 兼容的
  `POST {LLM_BASE_URL}/chat/completions` 接口，只依赖标准库 urllib。

  注意：LLM 分支在缺少 key、网络不通、超时（30s）、返回非 JSON、返回结构异常、
  内容为空等**任何**异常情况下，都会在 stderr 打印告警并**自动回退到离线模式**，
  保证程序不会因为远程调用失败而崩掉。

诚实声明：本仓库的开发环境没有 API key，LLM 分支只做了代码层面的异常兜底设计，
**没有在本机做过真实联网验证**（见 README「局限」）。
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from functools import lru_cache
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
PROMPTS_DIR = ROOT_DIR / "prompts"

ANSWER_TEMPLATE = "answer_template.md"
SYSTEM_PROMPT = "system_prompt.md"

REQUEST_TIMEOUT_SECONDS = 30.0
DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"

MODE_OFFLINE = "离线检索模式（TF-IDF 检索 + 模板拼装，不联网）"
MODE_LLM = "LLM 生成模式"
MODE_FALLBACK = "离线检索模式（LLM 调用失败，已自动回退）"


# --------------------------------------------------------------------- 基础工具
def _warn(message: str) -> None:
    """告警统一走 stderr，不污染可以重定向到文件的 stdout。"""
    print(f"[warn] {message}", file=sys.stderr, flush=True)


@lru_cache(maxsize=None)
def _read_prompt(filename: str) -> str:
    path = PROMPTS_DIR / filename
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FileNotFoundError(f"缺少提示词模板：{path}（{exc}）") from exc


def render_template(template: str, values: dict[str, str]) -> str:
    """把模板里的 {{key}} 替换成对应值；未知占位符原样保留，不抛异常。"""
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("{{" + key + "}}", value)
    return rendered


_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_MULTI_BLANK_RE = re.compile(r"\n{3,}")


def _clean(text: str) -> str:
    """去掉模板里的 HTML 注释（只在源码里给人看），并压掉多余空行。"""
    text = _HTML_COMMENT_RE.sub("", text)
    text = _MULTI_BLANK_RE.sub("\n\n", text)
    return text.strip("\n")


def format_references(hits: list[tuple[dict[str, Any], float]]) -> str:
    """把检索结果渲染成「参考」列表：标题 + id + 相似度。"""
    if not hits:
        return "_本次没有检索到相关文档。_"
    lines = []
    for rank, (doc, score) in enumerate(hits, start=1):
        lines.append(
            f"{rank}. **{doc.get('title', '(无标题)')}**"
            f"（`{doc.get('id', '?')}`，相似度 {score:.3f}）"
        )
    return "\n".join(lines)


def llm_config() -> dict[str, str] | None:
    """读取 LLM 相关环境变量；没有 key 就返回 None（即走离线模式）。"""
    api_key = os.environ.get("LLM_API_KEY", "").strip()
    if not api_key:
        return None
    return {
        "api_key": api_key,
        "base_url": (os.environ.get("LLM_BASE_URL", "").strip() or DEFAULT_BASE_URL).rstrip("/"),
        "model": os.environ.get("LLM_MODEL", "").strip() or DEFAULT_MODEL,
    }


# --------------------------------------------------------------------- 离线模式
def _offline_answer(query: str, hits: list[tuple[dict[str, Any], float]]) -> str:
    if not hits:
        return (
            "知识库里没有检索到相关问题。可以试试换个说法，"
            "或者用 `--interactive` 进去多问几轮。"
        )
    top_doc = hits[0][0]
    body = str(top_doc.get("answer", "")).strip() or "_该文档没有填写答案。_"
    return body


# --------------------------------------------------------------------- LLM 模式
def _build_user_message(query: str, hits: list[tuple[dict[str, Any], float]]) -> str:
    """把命中的知识库片段按 [1][2][3] 编号塞进 user message，方便模型引用。"""
    blocks = []
    for rank, (doc, score) in enumerate(hits, start=1):
        blocks.append(
            f"[{rank}] id={doc.get('id', '?')} 标题={doc.get('title', '')} "
            f"相似度={score:.3f}\n"
            f"问题：{doc.get('question', '')}\n"
            f"答案：{doc.get('answer', '')}"
        )
    context = "\n\n".join(blocks) if blocks else "（没有检索到相关片段）"
    return (
        f"用户问题：{query}\n\n"
        f"参考知识库片段：\n{context}\n\n"
        "请基于以上片段回答，给出可执行的排查步骤。"
    )


def _llm_answer(query: str, hits: list[tuple[dict[str, Any], float]], cfg: dict[str, str]) -> str:
    """调用 OpenAI 兼容的 /chat/completions。任何失败都抛异常，由上层回退。"""
    url = f"{cfg['base_url']}/chat/completions"
    payload = {
        "model": cfg["model"],
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": _read_prompt(SYSTEM_PROMPT)},
            {"role": "user", "content": _build_user_message(query, hits)},
        ],
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cfg['api_key']}",
        },
    )

    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        raw = response.read().decode("utf-8", errors="replace")

    parsed = json.loads(raw)  # 非 JSON -> JSONDecodeError
    content = parsed["choices"][0]["message"]["content"]  # 结构不对 -> KeyError/IndexError/TypeError
    if not isinstance(content, str) or not content.strip():
        raise ValueError("LLM 返回了空内容")
    return content.strip()


# --------------------------------------------------------------------- 对外入口
def generate(
    query: str,
    hits: list[tuple[dict[str, Any], float]],
    *,
    mode: str | None = None,
) -> str:
    """根据检索结果生成最终回答。

    :param query: 用户原始问题
    :param hits:  Retriever.search() 的返回值
    :param mode:  可选的强制模式，"offline" / "llm"；默认按环境变量自动判断
    :return:     已经套好模板的 Markdown 文本
    """
    template = _read_prompt(ANSWER_TEMPLATE)
    references = format_references(hits)

    cfg = None if mode == "offline" else llm_config()
    if cfg is None:
        answer = _offline_answer(query, hits)
        mode_label = MODE_OFFLINE
    else:
        mode_label = f"{MODE_LLM}（model={cfg['model']}）"
        try:
            answer = _llm_answer(query, hits, cfg)
        except urllib.error.URLError as exc:
            _warn(f"LLM 调用失败（网络层）：{exc}，已回退到离线模式")
            answer, mode_label = _offline_answer(query, hits), MODE_FALLBACK
        except TimeoutError:
            _warn(f"LLM 调用超时（>{REQUEST_TIMEOUT_SECONDS:.0f}s），已回退到离线模式")
            answer, mode_label = _offline_answer(query, hits), MODE_FALLBACK
        except json.JSONDecodeError as exc:
            _warn(f"LLM 返回内容不是合法 JSON：{exc}，已回退到离线模式")
            answer, mode_label = _offline_answer(query, hits), MODE_FALLBACK
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            _warn(f"LLM 返回结构异常：{exc!r}，已回退到离线模式")
            answer, mode_label = _offline_answer(query, hits), MODE_FALLBACK
        except Exception as exc:  # noqa: BLE001 - 最后一道兜底，绝不让远程调用打死主流程
            _warn(f"LLM 调用出现未预期异常：{exc!r}，已回退到离线模式")
            answer, mode_label = _offline_answer(query, hits), MODE_FALLBACK

    return _clean(
        render_template(
            template,
            {
                "mode": mode_label,
                "query": query.strip(),
                "answer": answer,
                "references": references,
            },
        )
    )
