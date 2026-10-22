#!/usr/bin/env python3
"""
IBKR Flex Web Service 客户端 - 内部模块（不直接执行）

封装 IBKR Flex Query API 的两步调用流程，提供统一的成交/持仓/现金流查询接口。
被 CLI 脚本 import 使用。

环境变量（从 workspace .env 读取）:
- IBKR_FLEX_TOKEN: Flex Web Service Token
- IBKR_FLEX_QUERY_ID: 已配置的 Activity Flex Query ID

API 流程:
1. SendRequest -> 返回 ReferenceCode
2. 等待数秒（IBKR 后端生成报告）
3. GetStatement -> 返回 XML

缓存策略:
- 默认 1 小时缓存到 ~/.cache/ibkr-flex/<hash>.xml
- 避免 IBKR Flex API 限频（每个 query 同 token 1 分钟最多 1 次）
"""
import os
import sys
import time
import hashlib
import tempfile
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional, Dict, List, Tuple

BASE = "https://gdcdyn.interactivebrokers.com/Universal/servlet/FlexStatementService"
CACHE_DIR = Path.home() / ".cache" / "ibkr-flex"
CACHE_TTL = 3600  # 1 hour
POLL_INTERVAL = 3


class FlexResponseError(RuntimeError):
    """Flex returned a response that is not a usable statement."""


class FlexStatementPending(FlexResponseError):
    """Flex accepted the query but has not finished generating the statement."""


def _local_name(tag: str) -> str:
    """Return an ElementTree tag without its optional XML namespace."""
    return tag.rsplit("}", 1)[-1]


def _direct_text(root: ET.Element, name: str) -> Optional[str]:
    for child in root:
        if _local_name(child.tag) == name:
            return (child.text or "").strip()
    return None


def _parse_xml(xml_str: str, context: str) -> ET.Element:
    if not isinstance(xml_str, str) or not xml_str.strip():
        raise FlexResponseError(f"{context} returned an empty response")
    try:
        return ET.fromstring(xml_str)
    except ET.ParseError as exc:
        raise FlexResponseError(f"{context} returned invalid XML: {exc}") from exc


def _error_detail(root: ET.Element) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    return (
        _direct_text(root, "Status"),
        _direct_text(root, "ErrorCode"),
        _direct_text(root, "ErrorMessage"),
    )


def _is_pending(status: Optional[str], code: Optional[str], message: Optional[str]) -> bool:
    """Recognize IBKR's documented/in-use statement-generation response."""
    if (status or "").lower() not in {"warn", "warning", "error", "fail", "failed"}:
        return False
    text = (message or "").lower()
    return code == "1019" or any(
        phrase in text
        for phrase in ("generation in progress", "still generating", "try again shortly")
    )


def validate_flex_statement(xml_str: str) -> ET.Element:
    """Validate that *xml_str* is a completed FlexQueryResponse.

    Flex error responses are well-formed XML too.  Treating them as reports makes
    all parsers return empty lists, which can turn a failed download into a false
    successful reconciliation.  Keep validation at the parser boundary as well
    as the cache/fetch boundary so callers cannot accidentally bypass it.
    """
    root = _parse_xml(xml_str, "Flex GetStatement")
    status, code, message = _error_detail(root)
    if status and status.lower() != "success":
        detail = message or "no error message"
        exc_type = FlexStatementPending if _is_pending(status, code, message) else FlexResponseError
        raise exc_type(
            f"Flex GetStatement failed (status={status}, code={code or 'unknown'}): {detail}"
        )

    if _local_name(root.tag) != "FlexQueryResponse":
        raise FlexResponseError(
            f"Flex GetStatement returned unexpected root element: {_local_name(root.tag)}"
        )
    if not any(_local_name(node.tag) == "FlexStatement" for node in root.iter()):
        raise FlexResponseError("Flex GetStatement response contains no FlexStatement")
    return root


def load_env(workspace_dir: Optional[str] = None) -> Dict[str, str]:
    """Load credentials from .env file. Search order:
    1. Explicit workspace_dir/.env
    2. Current cwd /.env
    3. Up to 3 parents of cwd
    Process env vars take precedence (don't override).
    """
    candidates = []
    if workspace_dir:
        candidates.append(Path(workspace_dir) / ".env")
    candidates.append(Path.cwd() / ".env")
    parents = list(Path.cwd().parents)[:3]
    for p in parents:
        candidates.append(p / ".env")

    env = dict(os.environ)
    for f in candidates:
        if f.exists():
            with open(f) as fp:
                for line in fp:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        env.setdefault(k.strip(), v.strip())
    return env


def get_credentials(workspace_dir: Optional[str] = None) -> Tuple[str, str]:
    env = load_env(workspace_dir)
    token = env.get("IBKR_FLEX_TOKEN")
    qid = env.get("IBKR_FLEX_QUERY_ID")
    if not token or not qid:
        msg = (
            "ERROR: IBKR_FLEX_TOKEN or IBKR_FLEX_QUERY_ID not set.\n"
            "Configure in workspace .env:\n"
            "  IBKR_FLEX_TOKEN=<token from IBKR Flex Web Service config>\n"
            "  IBKR_FLEX_QUERY_ID=<id from your Activity Flex Query>\n"
        )
        print(msg, file=sys.stderr)
        sys.exit(1)
    return token, qid


def _http_get(url: str, timeout: int = 30) -> str:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.read().decode()
    except urllib.error.URLError as e:
        raise RuntimeError(f"Flex API request failed: {e}")


def fetch_flex_xml(token: str, query_id: str, max_wait: int = 30) -> str:
    """Two-step Flex query. Returns full XML string."""
    # Step 1: SendRequest
    req1 = f"{BASE}.SendRequest?t={token}&q={query_id}&v=3"
    xml1 = _http_get(req1)
    root = _parse_xml(xml1, "Flex SendRequest")
    status, err, msg = _error_detail(root)
    if status != "Success":
        raise FlexResponseError(
            f"Flex SendRequest failed (status={status or 'missing'}, "
            f"code={err or 'unknown'}): {msg or 'no error message'}"
        )
    ref = _direct_text(root, "ReferenceCode")
    url2 = _direct_text(root, "Url")
    if not ref or not url2:
        raise FlexResponseError("Flex SendRequest succeeded without ReferenceCode or Url")

    # Step 2: GetStatement (poll until ready)
    waited = 0
    last_err: Optional[str] = None
    while waited < max_wait:
        delay = min(POLL_INTERVAL, max_wait - waited)
        time.sleep(delay)
        waited += delay
        try:
            xml2 = _http_get(f"{url2}?q={ref}&t={token}&v=3")
        except RuntimeError as e:
            last_err = str(e)
            continue

        try:
            validate_flex_statement(xml2)
        except FlexStatementPending as e:
            last_err = str(e)
            continue
        # All other Warn/Error/malformed/unexpected responses fail immediately.
        return xml2
    raise RuntimeError(f"Flex GetStatement timed out after {waited}s: {last_err}")


def _cache_key(token: str, query_id: str) -> Path:
    h = hashlib.md5(f"{token}:{query_id}".encode()).hexdigest()[:12]
    return CACHE_DIR / f"{h}.xml"


def get_flex_xml(token: str, query_id: str, force_refresh: bool = False) -> str:
    """Cached Flex XML pull (1h TTL)."""
    cache_file = _cache_key(token, query_id)
    if not force_refresh and cache_file.exists():
        age = time.time() - cache_file.stat().st_mtime
        if age < CACHE_TTL:
            cached = cache_file.read_text()
            try:
                validate_flex_statement(cached)
                return cached
            except FlexResponseError:
                # Old versions could cache a Warn/1019 response. Remove it and
                # fetch again rather than silently parsing it as an empty report.
                cache_file.unlink(missing_ok=True)

    xml_str = fetch_flex_xml(token, query_id)
    validate_flex_statement(xml_str)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    # Atomic replacement prevents readers from observing a partial XML file.
    with tempfile.NamedTemporaryFile(
        "w", dir=CACHE_DIR, prefix=f".{cache_file.name}.", delete=False
    ) as fp:
        fp.write(xml_str)
        temp_path = Path(fp.name)
    try:
        os.replace(temp_path, cache_file)
    finally:
        temp_path.unlink(missing_ok=True)
    return xml_str


def _f(val: str, default: float = 0.0) -> float:
    try:
        return float(val) if val not in (None, "", "-") else default
    except (ValueError, TypeError):
        return default


def parse_trades(xml_str: str, symbol: Optional[str] = None) -> List[Dict]:
    """Extract Trade nodes from Flex XML."""
    root = validate_flex_statement(xml_str)
    trades = []
    for t in root.findall(".//Trade"):
        a = t.attrib
        if symbol and a.get("symbol") != symbol:
            continue
        trades.append({
            "date": a.get("tradeDate", ""),
            "time": a.get("dateTime", ""),
            "symbol": a.get("symbol", ""),
            "description": a.get("description", ""),
            "qty": _f(a.get("quantity")),
            "price": _f(a.get("tradePrice")),
            "proceeds": _f(a.get("proceeds")),
            "commission": _f(a.get("ibCommission")),
            "orderType": a.get("orderType", ""),
            "transactionType": a.get("transactionType", ""),
            "notes": a.get("notes", ""),
            "assetCategory": a.get("assetCategory", ""),
            "currency": a.get("currency", ""),
            "putCall": a.get("putCall", ""),
            "strike": a.get("strike", ""),
            "expiry": a.get("expiry", ""),
            "exchange": a.get("exchange", ""),
        })
    trades.sort(key=lambda x: x["date"])
    return trades


def parse_open_positions(xml_str: str, symbol: Optional[str] = None) -> List[Dict]:
    """Extract OpenPosition nodes (current holdings snapshot)."""
    root = validate_flex_statement(xml_str)
    positions = []
    for p in root.findall(".//OpenPosition"):
        a = p.attrib
        if symbol and a.get("symbol") != symbol:
            continue
        positions.append({
            "symbol": a.get("symbol", ""),
            "description": a.get("description", ""),
            "qty": _f(a.get("position")),
            "avgCost": _f(a.get("costBasisPrice")),
            "totalCost": _f(a.get("costBasisMoney")),
            "markPrice": _f(a.get("markPrice")),
            "fifoPnlUnrealized": _f(a.get("fifoPnlUnrealized")),
            "reportDate": a.get("reportDate", ""),
            "currency": a.get("currency", ""),
            "assetCategory": a.get("assetCategory", ""),
            "putCall": a.get("putCall", ""),
            "strike": a.get("strike", ""),
            "expiry": a.get("expiry", ""),
        })
    return positions


def parse_cash_flow(xml_str: str) -> List[Dict]:
    """Extract CashTransaction nodes (deposits, dividends, fees, etc.)."""
    root = validate_flex_statement(xml_str)
    out = []
    for n in root.findall(".//CashTransaction"):
        a = n.attrib
        out.append({
            "date": a.get("dateTime", "")[:10],
            "type": a.get("type", ""),
            "symbol": a.get("symbol", ""),
            "description": a.get("description", ""),
            "amount": _f(a.get("amount")),
            "currency": a.get("currency", ""),
        })
    out.sort(key=lambda x: x["date"])
    return out
